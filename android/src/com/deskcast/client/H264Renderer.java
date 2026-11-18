package com.deskcast.client;

import android.media.MediaCodec;
import android.media.MediaFormat;
import android.os.Build;
import android.view.Surface;

import java.nio.ByteBuffer;

public class H264Renderer {
    public interface Callback {
        void onFrameRendered();
        void onError(String msg);
    }

    private final Callback callback;
    private MediaCodec decoder;
    private Surface surface;
    private Thread drainThread;
    private volatile boolean draining = false;
    private int curW = 0;
    private int curH = 0;
    private boolean waitingKey = true;
    private long ptsUs = 0;

    public H264Renderer(Callback callback) {
        this.callback = callback;
    }

    public synchronized void setSurface(Surface s) {
        if (this.surface != s) {
            release();
            this.surface = s;
            if (s != null && s.isValid() && Build.VERSION.SDK_INT >= 30) {
                try {
                    s.setFrameRate(60f, Surface.FRAME_RATE_COMPATIBILITY_DEFAULT);
                } catch (Exception ignored) {}
            }
        }
    }

    public synchronized void reset() {
        release();
        waitingKey = true;
    }

    public void release() {
        Thread dt;
        MediaCodec dec;
        synchronized (this) {
            draining = false;
            dt = drainThread;
            drainThread = null;
            dec = decoder;
            decoder = null;
            curW = 0;
            curH = 0;
            waitingKey = true;
        }
        if (dt != null) {
            dt.interrupt();
            try { dt.join(300); } catch (InterruptedException ignored) {}
        }
        if (dec != null) {
            try { dec.stop(); } catch (Exception ignored) {}
            try { dec.release(); } catch (Exception ignored) {}
        }
    }

    public synchronized void decodeFrame(int w, int h, boolean isKey, byte[] nal, int off, int len) {
        if (surface == null || !surface.isValid()) return;
        if (waitingKey && !isKey) return;

        try {
            if (decoder == null || curW != w || curH != h) {
                if (!isKey) return;
                release();
                MediaFormat fmt = MediaFormat.createVideoFormat("video/avc", w, h);
                setCsdFromAnnexB(fmt, nal, off, len);
                fmt.setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, Math.max(512 * 1024, len * 2));
                try { fmt.setInteger(MediaFormat.KEY_OPERATING_RATE, 120); } catch (Exception ignored) {}
                if (Build.VERSION.SDK_INT >= 30) {
                    try { fmt.setInteger(MediaFormat.KEY_LOW_LATENCY, 1); } catch (Exception ignored) {}
                }
                try { fmt.setInteger(MediaFormat.KEY_PRIORITY, 0); } catch (Exception ignored) {}

                decoder = MediaCodec.createDecoderByType("video/avc");
                decoder.configure(fmt, surface, null, 0);
                decoder.start();
                curW = w;
                curH = h;
                waitingKey = false;
                startDrainLoop(decoder);
            }

            int waitUs = isKey ? 20000 : 4000;
            int inIdx = decoder.dequeueInputBuffer(waitUs);
            if (inIdx >= 0) {
                ByteBuffer inBuf = decoder.getInputBuffer(inIdx);
                if (inBuf != null) {
                    inBuf.clear();
                    inBuf.put(nal, off, len);
                    ptsUs += 16666;
                    int flags = isKey ? MediaCodec.BUFFER_FLAG_KEY_FRAME : 0;
                    decoder.queueInputBuffer(inIdx, 0, len, ptsUs, flags);
                }
            }
        } catch (Exception e) {
            String msg = e.getMessage() != null ? e.getMessage() : e.getClass().getSimpleName();
            release();
            if (callback != null) callback.onError("MediaCodec: " + msg);
        }
    }

    private void startDrainLoop(final MediaCodec codecRef) {
        draining = true;
        drainThread = new Thread(() -> {
            MediaCodec.BufferInfo info = new MediaCodec.BufferInfo();
            while (draining) {
                try {
                    int outIdx = codecRef.dequeueOutputBuffer(info, 20000);
                    if (outIdx >= 0) {
                        codecRef.releaseOutputBuffer(outIdx, true);
                        if (callback != null) callback.onFrameRendered();
                    }
                } catch (IllegalStateException e) {
                    break;
                } catch (Exception e) {
                    if (draining && callback != null) {
                        callback.onError("Drain: " + e.getMessage());
                    }
                    break;
                }
            }
        }, "h264-drain");
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
