package com.deskcast.client;

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
import java.util.Random;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

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
    private final Random rng = new SecureRandom();
    private final ExecutorService sendExec = Executors.newSingleThreadExecutor();

    public WsClient(String host, int port, boolean useTls, Listener listener) {
        this.host = host;
        this.port = port;
        this.useTls = useTls;
        this.listener = listener;
    }

    public void start() {
        running = true;
        new Thread(this::runLoop, "ws-reader").start();
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
        String closeReason = "Desconectado";
        try {
            socket = createSocket();
            socket.setSoTimeout(6000);
            InputStream in = new BufferedInputStream(socket.getInputStream(), 128 * 1024);
            out = socket.getOutputStream();

            byte[] keyBytes = new byte[16];
            rng.nextBytes(keyBytes);
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
            ByteArrayOutputStream hdr = new ByteArrayOutputStream(16 + n);
            hdr.write(0x80 | opcode);
            if (n < 126) {
                hdr.write(0x80 | n);
            } else if (n < 65536) {
                hdr.write(0x80 | 126);
                hdr.write((n >> 8) & 0xFF);
                hdr.write(n & 0xFF);
            } else {
                hdr.write(0x80 | 127);
                for (int i = 7; i >= 0; i--) hdr.write((int) ((n >> (i * 8)) & 0xFF));
            }
            byte[] mask = new byte[4];
            rng.nextBytes(mask);
            hdr.write(mask);
            byte[] masked = new byte[n];
            for (int i = 0; i < n; i++) masked[i] = (byte) (payload[i] ^ mask[i % 4]);
            hdr.write(masked);
            out.write(hdr.toByteArray());
            out.flush();
        }
    }
}
