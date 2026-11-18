package com.deskcast.client;

import android.os.Process;
import android.util.Base64;

import java.io.BufferedInputStream;
import java.io.ByteArrayOutputStream;
import java.io.EOFException;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.security.cert.X509Certificate;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ThreadLocalRandom;

import javax.net.ssl.SSLContext;
import javax.net.ssl.SSLSocket;
import javax.net.ssl.TrustManager;
import javax.net.ssl.X509TrustManager;

public class WsClient {
    public interface Listener {
        void onOpen();
        void onText(String text);
        void onBinary(byte[] data);
        void onClose(String reason);
    }

    private final String host;
    private final int port;
    private final boolean useTls;
    private final Listener listener;

    private volatile boolean running = false;
    private Socket socket;
    private OutputStream out;
    private final Object writeLock = new Object();
    private final ExecutorService sendExec = Executors.newSingleThreadExecutor(r -> {
        Thread t = new Thread(r, "ws-send");
        t.setPriority(Thread.MAX_PRIORITY);
        return t;
    });

    public WsClient(String host, int port, boolean useTls, Listener listener) {
        this.host = host;
        this.port = port;
        this.useTls = useTls;
        this.listener = listener;
    }

    public void start() {
        running = true;
        Thread t = new Thread(this::runLoop, "ws-reader");
        t.setPriority(Thread.MAX_PRIORITY);
        t.start();
    }

    public void close() {
        running = false;
        try {
            if (socket != null) socket.close();
        } catch (Exception ignored) {
        }
    }

    private Socket createSocket() throws Exception {
        Socket raw = new Socket();
        raw.setTcpNoDelay(true);
        raw.setKeepAlive(true);
        try { raw.setPerformancePreferences(0, 2, 1); } catch (Exception ignored) {}
        try { raw.setTrafficClass(0x10); } catch (Exception ignored) {}
        raw.setReceiveBufferSize(512 * 1024);
        raw.connect(new InetSocketAddress(host, port), 5000);
        if (!useTls) return raw;

        TrustManager[] trustAll = new TrustManager[]{
            new X509TrustManager() {
                public X509Certificate[] getAcceptedIssuers() { return new X509Certificate[0]; }
                public void checkClientTrusted(X509Certificate[] certs, String authType) {}
                public void checkServerTrusted(X509Certificate[] certs, String authType) {}
            }
        };
        SSLContext ctx = SSLContext.getInstance("TLS");
        ctx.init(null, trustAll, new SecureRandom());
        SSLSocket ssl = (SSLSocket) ctx.getSocketFactory().createSocket(raw, host, port, true);
        ssl.startHandshake();
        return ssl;
    }

    private void runLoop() {
        try { Process.setThreadPriority(Process.THREAD_PRIORITY_URGENT_DISPLAY); } catch (Throwable ignored) {}
        String closeReason = "Desconectado";
        try {
            socket = createSocket();
            socket.setSoTimeout(6000);
            InputStream in = new BufferedInputStream(socket.getInputStream(), 128 * 1024);
            out = socket.getOutputStream();

            byte[] keyBytes = new byte[16];
            ThreadLocalRandom.current().nextBytes(keyBytes);
            String key = Base64.encodeToString(keyBytes, Base64.NO_WRAP);
            String req =
                "GET /ws HTTP/1.1\r\n" +
                "Host: " + host + ":" + port + "\r\n" +
                "Upgrade: websocket\r\n" +
                "Connection: Upgrade\r\n" +
                "Sec-WebSocket-Key: " + key + "\r\n" +
                "Sec-WebSocket-Version: 13\r\n\r\n";
            out.write(req.getBytes(StandardCharsets.US_ASCII));
            out.flush();

            String headers = readHttpHeaders(in);
            if (!headers.contains("101")) {
                throw new IOException("Handshake fallido: " + headers);
            }
            socket.setSoTimeout(0);
            listener.onOpen();

            while (running) {
                int b1 = in.read();
                if (b1 < 0) break;
                int b2 = in.read();
                if (b2 < 0) break;

                int opcode = b1 & 0x0F;
                boolean masked = (b2 & 0x80) != 0;
                long len = b2 & 0x7F;
                if (len == 126) {
                    byte[] ext = readExact(in, 2);
                    len = ((ext[0] & 0xFF) << 8) | (ext[1] & 0xFF);
                } else if (len == 127) {
                    byte[] ext = readExact(in, 8);
                    len = 0;
                    for (int i = 0; i < 8; i++) len = (len << 8) | (ext[i] & 0xFFL);
                }
                if (len < 0 || len > 16 * 1024 * 1024) {
                    throw new IOException("Frame demasiado grande: " + len);
                }
                byte[] mask = masked ? readExact(in, 4) : null;
                byte[] payload = readExact(in, (int) len);
                if (mask != null) {
                    for (int i = 0; i < payload.length; i++) payload[i] ^= mask[i % 4];
                }

                if (opcode == 0x08) {
                    closeReason = "Conexión cerrada por el servidor";
                    break;
                } else if (opcode == 0x09) {
                    sendFrame(0x0A, payload);
                } else if (opcode == 0x01) {
                    listener.onText(new String(payload, StandardCharsets.UTF_8));
                } else if (opcode == 0x02) {
                    listener.onBinary(payload);
                }
            }
        } catch (Exception e) {
            if (running) closeReason = e.getMessage() != null ? e.getMessage() : e.getClass().getSimpleName();
        } finally {
            close();
            listener.onClose(closeReason);
        }
    }

    private String readHttpHeaders(InputStream in) throws IOException {
        ByteArrayOutputStream buf = new ByteArrayOutputStream();
        int state = 0;
        while (true) {
            int b = in.read();
            if (b < 0) throw new EOFException("EOF durante handshake");
            buf.write(b);
            if ((state == 0 || state == 2) && b == '\r') state++;
            else if (state == 1 && b == '\n') state = 2;
            else if (state == 3 && b == '\n') break;
            else state = (b == '\r') ? 1 : 0;
            if (buf.size() > 8192) throw new IOException("Cabecera HTTP demasiado larga");
        }
        return buf.toString("ISO-8859-1");
    }

    private byte[] readExact(InputStream in, int len) throws IOException {
        byte[] data = new byte[len];
        int off = 0;
        while (off < len) {
            int r = in.read(data, off, len - off);
            if (r < 0) throw new EOFException("Stream cerrado");
            off += r;
        }
        return data;
    }

    public void sendText(String text) {
        if (!running || out == null) return;
        byte[] payload = text.getBytes(StandardCharsets.UTF_8);
        sendExec.execute(() -> {
            try {
                sendFrame(0x01, payload);
            } catch (Exception ignored) {
            }
        });
    }

    private void sendFrame(int opcode, byte[] payload) throws IOException {
        synchronized (writeLock) {
            if (out == null) return;
            int n = payload.length;
            int hdrLen = 2 + (n < 126 ? 0 : (n < 65536 ? 2 : 8)) + 4;
            byte[] pkt = new byte[hdrLen + n];
            pkt[0] = (byte) (0x80 | opcode);
            int p = 2;
            if (n < 126) {
                pkt[1] = (byte) (0x80 | n);
            } else if (n < 65536) {
                pkt[1] = (byte) (0x80 | 126);
                pkt[2] = (byte) ((n >> 8) & 0xFF);
                pkt[3] = (byte) (n & 0xFF);
                p = 4;
            } else {
                pkt[1] = (byte) (0x80 | 127);
                for (int i = 7; i >= 0; i--) pkt[p++] = (byte) ((n >> (i * 8)) & 0xFF);
            }
            int maskVal = ThreadLocalRandom.current().nextInt();
            byte m0 = (byte) (maskVal & 0xFF);
            byte m1 = (byte) ((maskVal >> 8) & 0xFF);
            byte m2 = (byte) ((maskVal >> 16) & 0xFF);
            byte m3 = (byte) ((maskVal >> 24) & 0xFF);
            pkt[p] = m0; pkt[p + 1] = m1; pkt[p + 2] = m2; pkt[p + 3] = m3;
            byte[] mask = new byte[]{m0, m1, m2, m3};
            int off = p + 4;
            for (int i = 0; i < n; i++) pkt[off + i] = (byte) (payload[i] ^ mask[i & 3]);
            out.write(pkt);
            out.flush();
        }
    }
}
