package com.deskcast.client;

import android.media.MediaCodec;
import android.media.MediaFormat;
import android.os.Build;
import android.os.Process;
import android.view.Surface;

import java.nio.ByteBuffer;
import java.util.concurrent.LinkedBlockingQueue;

public class H264Renderer {
    public interface Callback {
        void onFrameRendered();
        void onError(String msg);
        default void onRequestKeyframe() {}
    }

    private static final class FrameTask {
        final int w, h;
        final boolean isKey;
        final byte[] data;
        final int off, len;

        FrameTask(int w, int h, boolean isKey, byte[] data, int off, int len) {
            this.w = w;
            this.h = h;
            this.isKey = isKey;
            this.data = data;
            this.off = off;
            this.len = len;
        }
    }

    private final Callback callback;
    // Cola amplia: NUNCA descartar P-frames sueltos en H.264 porque rompe la referencia de los siguientes frames
    private final LinkedBlockingQueue<FrameTask> queue = new LinkedBlockingQueue<>(96);
    private final Object codecLock = new Object();

    private MediaCodec decoder;
    private Surface surface;
    private Thread feedThread;
    private Thread drainThread;
    private volatile boolean active = false;
    private int curW = 0;
    private int curH = 0;
    private float displayHz = 60f;
    private long lastKfReqMs = 0L;
    private volatile boolean sawKeyEnqueued = false;
    private long startNs = 0;

    public H264Renderer(Callback callback) {
        this.callback = callback;
    }

    public void setDisplayRefreshRate(float hz) {
        if (hz <= 0f) return;
        synchronized (codecLock) {
            this.displayHz = hz;
            applySurfaceFrameRateLocked(this.surface);
        }
    }

    private void applySurfaceFrameRateLocked(Surface s) {
        if (s == null || !s.isValid() || Build.VERSION.SDK_INT < 30) return;
        try {
            // En pantallas de 90 Hz (no múltiplo de 60), pedir 60f fuerza 45 Hz en SurfaceFlinger.
            // Pedimos la tasa nativa (p.ej. 90f) salvo que 60 sea divisor exacto (60/120 Hz).
            int rounded = Math.round(displayHz);
            float target = (rounded >= 60 && rounded % 60 != 0) ? displayHz : 60f;
            s.setFrameRate(target, Surface.FRAME_RATE_COMPATIBILITY_DEFAULT);
        } catch (Exception ignored) {}
    }

    public void setSurface(Surface s) {
        synchronized (codecLock) {
            if (this.surface != s) {
                release();
                this.surface = s;
                applySurfaceFrameRateLocked(s);
            }
        }
    }

    public void reset() {
        release();
    }

    public void release() {
        Thread ft, dt;
        MediaCodec dec;
        synchronized (codecLock) {
            active = false;
            ft = feedThread;
            dt = drainThread;
            feedThread = null;
            drainThread = null;
            dec = decoder;
            decoder = null;
            curW = 0;
            curH = 0;
            sawKeyEnqueued = false;
            queue.clear();
        }
        if (ft != null) {
            ft.interrupt();
            try { ft.join(250); } catch (InterruptedException ignored) {}
        }
        if (dt != null) {
            dt.interrupt();
            try { dt.join(250); } catch (InterruptedException ignored) {}
        }
        if (dec != null) {
            try { dec.stop(); } catch (Exception ignored) {}
            try { dec.release(); } catch (Exception ignored) {}
        }
    }

    public void decodeFrame(int w, int h, boolean isKey, byte[] nal, int off, int len) {
        if (isKey) sawKeyEnqueued = true;
        if (!sawKeyEnqueued) return;
        synchronized (codecLock) {
            if (surface == null || !surface.isValid()) return;
            if (!active || feedThread == null) {
                active = true;
                startNs = System.nanoTime();
                feedThread = new Thread(this::feedLoop, "h264-feed");
                feedThread.setPriority(Thread.MAX_PRIORITY);
                feedThread.start();
            }
        }

        FrameTask task = new FrameTask(w, h, isKey, nal, off, len);
        if (isKey) {
            // Si llega un keyframe nuevo y hay cola atrasada, se puede limpiar todo hasta él sin romper nada
            if (queue.size() > 2) queue.clear();
            queue.offer(task);
        } else if (!queue.offer(task)) {
            // Si la cola de 96 frames se llenara por bloqueo extremo, esperar al siguiente keyframe
            sawKeyEnqueued = false;
            queue.clear();
            if (callback != null) callback.onRequestKeyframe();
        } else if (queue.size() >= 8 && callback != null) {
            long nowMs = System.currentTimeMillis();
            if (nowMs - lastKfReqMs > 400L) {
                lastKfReqMs = nowMs;
                callback.onRequestKeyframe();
            }
        }
    }

    private void feedLoop() {
        try { Process.setThreadPriority(Process.THREAD_PRIORITY_URGENT_DISPLAY); } catch (Throwable ignored) {}
        while (active) {
            try {
                FrameTask t = queue.take();
                processFrame(t);
            } catch (InterruptedException e) {
                break;
            }
        }
    }

    private void processFrame(FrameTask t) {
        try {
            MediaCodec dec;
            synchronized (codecLock) {
                if (!active || surface == null || !surface.isValid()) return;

                if (decoder == null || curW != t.w || curH != t.h) {
                    if (!t.isKey) return;
                    stopDecoderLocked();

                    MediaFormat fmt = MediaFormat.createVideoFormat("video/avc", t.w, t.h);
                    setCsdFromAnnexB(fmt, t.data, t.off, t.len);
                    fmt.setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, Math.max(512 * 1024, t.len * 2));
                    try { fmt.setInteger(MediaFormat.KEY_OPERATING_RATE, 120); } catch (Exception ignored) {}
                    if (Build.VERSION.SDK_INT >= 30) {
                        try { fmt.setInteger(MediaFormat.KEY_LOW_LATENCY, 1); } catch (Exception ignored) {}
                    }
                    if (Build.VERSION.SDK_INT >= 31) {
                        try { fmt.setInteger(MediaFormat.KEY_ALLOW_FRAME_DROP, 1); } catch (Exception ignored) {}
                    }
                    try { fmt.setInteger(MediaFormat.KEY_PRIORITY, 0); } catch (Exception ignored) {}

                    decoder = MediaCodec.createDecoderByType("video/avc");
                    decoder.configure(fmt, surface, null, 0);
                    decoder.start();
                    curW = t.w;
                    curH = t.h;
                    startDrainLoopLocked(decoder);
                }
                dec = decoder;
            }

            if (dec == null) return;
            int inIdx = dec.dequeueInputBuffer(10000);
            if (inIdx >= 0) {
                ByteBuffer inBuf = dec.getInputBuffer(inIdx);
                if (inBuf != null) {
                    inBuf.clear();
                    inBuf.put(t.data, t.off, t.len);
                    long ptsUs = Math.max(0L, (System.nanoTime() - startNs) / 1000L);
                    int flags = t.isKey ? MediaCodec.BUFFER_FLAG_KEY_FRAME : 0;
                    dec.queueInputBuffer(inIdx, 0, t.len, ptsUs, flags);
                }
            }
        } catch (Exception e) {
            String msg = e.getMessage() != null ? e.getMessage() : e.getClass().getSimpleName();
            synchronized (codecLock) {
                stopDecoderLocked();
                sawKeyEnqueued = false;
            }
            if (callback != null) callback.onError("MediaCodec: " + msg);
        }
    }

    private void stopDecoderLocked() {
        Thread dt = drainThread;
        MediaCodec dec = decoder;
        drainThread = null;
        decoder = null;
        if (dt != null) dt.interrupt();
        if (dec != null) {
            try { dec.stop(); } catch (Exception ignored) {}
            try { dec.release(); } catch (Exception ignored) {}
        }
    }

    private void startDrainLoopLocked(final MediaCodec codecRef) {
        drainThread = new Thread(() -> {
            try { Process.setThreadPriority(Process.THREAD_PRIORITY_URGENT_DISPLAY); } catch (Throwable ignored) {}
            MediaCodec.BufferInfo info = new MediaCodec.BufferInfo();
            while (active) {
                try {
                    int outIdx = codecRef.dequeueOutputBuffer(info, 15000);
                    if (outIdx >= 0) {
                        // Si ya hay más frames decodificados listos, descartar los intermedios y mostrar solo el más reciente
                        int latestIdx = outIdx;
                        while (true) {
                            int nextIdx = codecRef.dequeueOutputBuffer(info, 0);
                            if (nextIdx >= 0) {
                                codecRef.releaseOutputBuffer(latestIdx, false);
                                latestIdx = nextIdx;
                                if (callback != null) callback.onFrameRendered();
                            } else {
                                break;
                            }
                        }
                        codecRef.releaseOutputBuffer(latestIdx, true);
                        if (callback != null) callback.onFrameRendered();
                    }
                } catch (IllegalStateException e) {
                    break;
                } catch (Exception e) {
                    if (active && callback != null) {
                        callback.onError("Drain: " + e.getMessage());
                    }
                    break;
                }
            }
        }, "h264-drain");
        drainThread.setPriority(Thread.MAX_PRIORITY);
        drainThread.start();
    }

    private void setCsdFromAnnexB(MediaFormat fmt, byte[] data, int off, int len) {
        int end = off + len;
        int curStart = -1;
        int curType = -1;
        byte[] sps = null;
        byte[] pps = null;

        for (int i = off; i <= end - 4; i++) {
            int scLen = 0;
            if (data[i] == 0 && data[i + 1] == 0) {
                if (data[i + 2] == 1) scLen = 3;
                else if (i + 3 < end && data[i + 2] == 0 && data[i + 3] == 1) scLen = 4;
            }
            if (scLen > 0) {
                if (curStart >= 0) {
                    int nLen = i - curStart;
                    if (curType == 7 && sps == null && nLen > 4) {
                        sps = new byte[nLen];
                        System.arraycopy(data, curStart, sps, 0, nLen);
                    } else if (curType == 8 && pps == null && nLen > 4) {
                        pps = new byte[nLen];
                        System.arraycopy(data, curStart, pps, 0, nLen);
                    }
                }
                curStart = i;
                int nalIdx = i + scLen;
                curType = (nalIdx < end) ? (data[nalIdx] & 0x1F) : -1;
                i += scLen - 1;
            }
        }
        if (sps != null) fmt.setByteBuffer("csd-0", ByteBuffer.wrap(sps));
        if (pps != null) fmt.setByteBuffer("csd-1", ByteBuffer.wrap(pps));
    }
}
