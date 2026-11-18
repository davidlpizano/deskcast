package com.deskcast.client;

import android.app.Activity;
import android.content.Context;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.Editable;
import android.text.InputType;
import android.text.TextWatcher;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.KeyEvent;
import android.view.MotionEvent;
import android.view.SurfaceHolder;
import android.view.SurfaceView;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.view.WindowManager;
import android.view.inputmethod.InputMethodManager;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.Spinner;
import android.widget.TextView;

import org.json.JSONObject;

public class MainActivity extends Activity implements SurfaceHolder.Callback {
    private static final String PREFS = "deskcast_prefs";

    private FrameLayout root;
    private SurfaceView surfaceView;
    private TextView statusBadge;
    private TextView fabToggle, fabKbd;
    private EditText kbdBridge;
    private LinearLayout panelWrap;
    private EditText editHost, editPort, editPin;
    private CheckBox chkHttps, chkTrackpad;
    private Spinner spinRes, spinPos, spinBitrate;
    private Button btnMirror, btnRClick;

    private final Handler ui = new Handler(Looper.getMainLooper());
    private volatile String lastCodecErr = "";
    private final H264Renderer renderer = new H264Renderer(new H264Renderer.Callback() {
        @Override
        public void onFrameRendered() {
            fpsCounter++;
            lastCodecErr = "";
        }

        @Override
        public void onError(String msg) {
            lastCodecErr = msg;
            ui.post(() -> statusBadge.setText(msg));
        }
    });
    private WsClient ws;
    private WifiManager.WifiLock wifiLock;

    private int streamW = 1280;
    private int streamH = 800;
    private boolean mirrorMode = false;
    private boolean rightClickArmed = false;
    private boolean userDisconnected = false;
    private boolean surfaceReady = false;
    private byte[] pendingKeyPacket = null;

    private volatile int fpsCounter = 0;
    private volatile long bytesCounter = 0;
    private volatile long lastPingMs = 0;

    private long lastMoveTs = 0;
    private float prevScrollY = 0;
    private boolean scrolling2F = false;
    private double trackX = 0.5, trackY = 0.5;
    private float lastTouchX = 0, lastTouchY = 0;

    private final Runnable statsTick = new Runnable() {
        @Override
        public void run() {
            int f = fpsCounter;
            long b = bytesCounter;
            fpsCounter = 0;
            bytesCounter = 0;
            long kbps = Math.round((b * 8.0) / 1000.0);
            String bw = kbps > 1000 ? String.format("%.1f Mbps", kbps / 1000.0) : (kbps + " kbps");
            if (!lastCodecErr.isEmpty()) {
                statusBadge.setText(lastCodecErr);
            } else {
                String prefix = (ws != null) ? (f == 0 ? "H.264 (reposo)" : "H.264 HW") : "Sin conexión";
                String lat = lastPingMs > 0 ? (" · " + lastPingMs + "ms") : "";
                statusBadge.setText(prefix + " · " + streamW + "×" + streamH + " · " + f + " FPS · " + bw + lat);
            }
            if (ws != null) {
                ws.sendText("{\"type\":\"ping\",\"t\":" + System.currentTimeMillis() + "}");
            }
            ui.postDelayed(this, 1000);
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        buildUi();
        enterFullscreen();
        ui.postDelayed(statsTick, 1000);

        SharedPreferences sp = getSharedPreferences(PREFS, MODE_PRIVATE);
        if (sp.contains("host")) {
            connectNow();
        }
    }

    private int dp(int v) {
        return Math.round(TypedValue.applyDimension(
            TypedValue.COMPLEX_UNIT_DIP, v, getResources().getDisplayMetrics()
        ));
    }

    private GradientDrawable roundedBg(int color, int radiusDp) {
        GradientDrawable gd = new GradientDrawable();
        gd.setColor(color);
        gd.setCornerRadius(dp(radiusDp));
        return gd;
    }

    private void buildUi() {
        root = new FrameLayout(this);
        root.setBackgroundColor(Color.BLACK);

        surfaceView = new SurfaceView(this);
        surfaceView.getHolder().addCallback(this);
        FrameLayout.LayoutParams svLp = new FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT, Gravity.CENTER
        );
        root.addView(surfaceView, svLp);
        surfaceView.setOnTouchListener((v, ev) -> handleSurfaceTouch(ev));

        // Input invisible para teclado móvil
        kbdBridge = new EditText(this);
        kbdBridge.setAlpha(0f);
        kbdBridge.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS);
        FrameLayout.LayoutParams kLp = new FrameLayout.LayoutParams(1, 1, Gravity.TOP | Gravity.START);
        root.addView(kbdBridge, kLp);
        setupKeyboardBridge();

        // Badge superior izquierdo
        statusBadge = new TextView(this);
        statusBadge.setText("DeskCast · Listo");
        statusBadge.setTextColor(Color.WHITE);
        statusBadge.setTextSize(12);
        statusBadge.setTypeface(Typeface.MONOSPACE);
        statusBadge.setPadding(dp(10), dp(5), dp(10), dp(5));
        statusBadge.setBackground(roundedBg(0xAA0F172A, 14));
        FrameLayout.LayoutParams bLp = new FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT, Gravity.TOP | Gravity.START
        );
        bLp.setMargins(dp(12), dp(12), 0, 0);
        root.addView(statusBadge, bLp);

        // Botón flotante teclado
        fabKbd = new TextView(this);
        fabKbd.setText("⌨");
        fabKbd.setTextColor(Color.WHITE);
        fabKbd.setTextSize(18);
        fabKbd.setGravity(Gravity.CENTER);
        fabKbd.setBackground(roundedBg(0xAA1E293B, 20));
        FrameLayout.LayoutParams fkLp = new FrameLayout.LayoutParams(dp(42), dp(42), Gravity.TOP | Gravity.END);
        fkLp.setMargins(0, dp(10), dp(62), 0);
        fabKbd.setOnClickListener(v -> openSoftKeyboard());
        root.addView(fabKbd, fkLp);

        // Botón flotante ⚙ superior derecho
        fabToggle = new TextView(this);
        fabToggle.setText("⚙");
        fabToggle.setTextColor(Color.WHITE);
        fabToggle.setTextSize(18);
        fabToggle.setGravity(Gravity.CENTER);
        fabToggle.setBackground(roundedBg(0xBB1E293B, 20));
        FrameLayout.LayoutParams fLp = new FrameLayout.LayoutParams(dp(42), dp(42), Gravity.TOP | Gravity.END);
        fLp.setMargins(0, dp(10), dp(12), 0);
        fabToggle.setOnClickListener(v -> togglePanel());
        root.addView(fabToggle, fLp);

        // Panel de conexión/ajustes
        panelWrap = new LinearLayout(this);
        panelWrap.setOrientation(LinearLayout.VERTICAL);
        panelWrap.setBackground(roundedBg(0xEE0F172A, 16));
        panelWrap.setPadding(dp(18), dp(16), dp(18), dp(16));

        ScrollView scroll = new ScrollView(this);
        LinearLayout card = new LinearLayout(this);
        card.setOrientation(LinearLayout.VERTICAL);
        scroll.addView(card);
        panelWrap.addView(scroll);

        TextView title = new TextView(this);
        title.setText("DeskCast · Cliente Nativo H.264");
        title.setTextColor(Color.WHITE);
        title.setTextSize(17);
        title.setTypeface(Typeface.DEFAULT_BOLD);
        card.addView(title);

        TextView sub = new TextView(this);
        sub.setText("Decodificación H.264 por hardware (MediaCodec) + Wi-Fi baja latencia");
        sub.setTextColor(0xFF94A3B8);
        sub.setTextSize(12);
        sub.setPadding(0, dp(2), 0, dp(10));
        card.addView(sub);

        LinearLayout presets = new LinearLayout(this);
        presets.setOrientation(LinearLayout.HORIZONTAL);
        presets.addView(makeChip("Hotspot 10.42.0.1", () -> {
            editHost.setText("10.42.0.1");
            editPort.setText("8765");
            chkHttps.setChecked(false);
        }));
        presets.addView(makeChip("USB 127.0.0.1", () -> {
            editHost.setText("127.0.0.1");
            editPort.setText("8765");
            chkHttps.setChecked(false);
        }));
        presets.addView(makeChip("Tailscale", () -> {
            editHost.setText("100.");
            editHost.setSelection(editHost.getText().length());
            editHost.requestFocus();
            editPort.setText("8765");
            chkHttps.setChecked(false);
        }));
        card.addView(presets);

        SharedPreferences sp = getSharedPreferences(PREFS, MODE_PRIVATE);

        LinearLayout rowHost = new LinearLayout(this);
        rowHost.setOrientation(LinearLayout.HORIZONTAL);
        rowHost.setPadding(0, dp(8), 0, 0);
        editHost = makeInput("IP del PC (ej. 10.42.0.1)", sp.getString("host", "10.42.0.1"));
        LinearLayout.LayoutParams hLp = new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 3f);
        hLp.setMargins(0, 0, dp(8), 0);
        rowHost.addView(editHost, hLp);

        editPort = makeInput("Puerto", sp.getString("port", "8765"));
        editPort.setInputType(InputType.TYPE_CLASS_NUMBER);
        rowHost.addView(editPort, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        card.addView(rowHost);

        LinearLayout rowPin = new LinearLayout(this);
        rowPin.setOrientation(LinearLayout.HORIZONTAL);
        rowPin.setGravity(Gravity.CENTER_VERTICAL);
        rowPin.setPadding(0, dp(6), 0, dp(6));
        editPin = makeInput("PIN (vacío si no hay)", sp.getString("pin", ""));
        editPin.setInputType(InputType.TYPE_CLASS_NUMBER | InputType.TYPE_NUMBER_VARIATION_PASSWORD);
        rowPin.addView(editPin, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 2f));
        chkHttps = new CheckBox(this);
        chkHttps.setText("HTTPS");
        chkHttps.setTextColor(Color.WHITE);
        chkHttps.setChecked(sp.getBoolean("https", false));
        rowPin.addView(chkHttps);

        chkTrackpad = new CheckBox(this);
        chkTrackpad.setText("Trackpad");
        chkTrackpad.setTextColor(Color.WHITE);
        chkTrackpad.setChecked(sp.getBoolean("trackpad", false));
        rowPin.addView(chkTrackpad);
        card.addView(rowPin);

        LinearLayout rowOpts = new LinearLayout(this);
        rowOpts.setOrientation(LinearLayout.HORIZONTAL);
        spinRes = makeSpinner(new String[]{"1024x640", "1280x800", "1600x1000", "1920x1200"}, 1);
        spinPos = makeSpinner(new String[]{"right", "left", "top", "bottom"}, 0);
        spinBitrate = makeSpinner(new String[]{"2500", "4000", "6000", "8000", "10000"}, 1);
        rowOpts.addView(wrapSpinner("Resolución", spinRes), new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        rowOpts.addView(wrapSpinner("Posición", spinPos), new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        rowOpts.addView(wrapSpinner("Bitrate kbps", spinBitrate), new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        card.addView(rowOpts);

        LinearLayout actions = new LinearLayout(this);
        actions.setOrientation(LinearLayout.HORIZONTAL);
        actions.setPadding(0, dp(12), 0, 0);

        Button btnConn = makeBtn("Conectar", 0xFF2563EB, v -> connectNow());
        Button btnApply = makeBtn("Aplicar", 0xFF0F766E, v -> sendConfig());
        btnMirror = makeBtn("Espejo: OFF", 0xFF334155, v -> {
            mirrorMode = !mirrorMode;
            btnMirror.setText("Espejo: " + (mirrorMode ? "ON" : "OFF"));
            if (ws != null) ws.sendText("{\"type\":\"config\",\"mirror\":" + mirrorMode + "}");
        });
        btnRClick = makeBtn("Clic der.", 0xFF334155, v -> {
            rightClickArmed = !rightClickArmed;
            btnRClick.setText(rightClickArmed ? "Clic der. (listo)" : "Clic der.");
        });
        Button btnHide = makeBtn("Ocultar", 0xFF334155, v -> panelWrap.setVisibility(View.GONE));

        actions.addView(btnConn);
        actions.addView(btnApply);
        actions.addView(btnMirror);
        actions.addView(btnRClick);
        actions.addView(btnHide);
        card.addView(actions);

        FrameLayout.LayoutParams pLp = new FrameLayout.LayoutParams(
            dp(540), ViewGroup.LayoutParams.WRAP_CONTENT, Gravity.CENTER
        );
        root.addView(panelWrap, pLp);

        setContentView(root);
        root.addOnLayoutChangeListener((v, l, t, r, b, ol, ot, or, ob) -> fitSurfaceAspect());
    }

    private void setupKeyboardBridge() {
        kbdBridge.setOnKeyListener((v, keyCode, event) -> {
            if (ws == null) return false;
            int ks = 0;
            if (keyCode == KeyEvent.KEYCODE_DEL) ks = 0xff08;
            else if (keyCode == KeyEvent.KEYCODE_ENTER) ks = 0xff0d;
            else if (keyCode == KeyEvent.KEYCODE_TAB) ks = 0xff09;
            else if (keyCode == KeyEvent.KEYCODE_ESCAPE) ks = 0xff1b;
            else if (keyCode == KeyEvent.KEYCODE_DPAD_LEFT) ks = 0xff51;
            else if (keyCode == KeyEvent.KEYCODE_DPAD_UP) ks = 0xff52;
            else if (keyCode == KeyEvent.KEYCODE_DPAD_RIGHT) ks = 0xff53;
            else if (keyCode == KeyEvent.KEYCODE_DPAD_DOWN) ks = 0xff54;
            if (ks != 0) {
                boolean pressed = (event.getAction() == KeyEvent.ACTION_DOWN);
                if (event.getAction() == KeyEvent.ACTION_DOWN || event.getAction() == KeyEvent.ACTION_UP) {
                    ws.sendText("{\"type\":\"key\",\"keysym\":" + ks + ",\"pressed\":" + pressed + "}");
                }
                return true;
            }
            return false;
        });

        kbdBridge.addTextChangedListener(new TextWatcher() {
            public void beforeTextChanged(CharSequence s, int start, int count, int after) {}
            public void onTextChanged(CharSequence s, int start, int before, int count) {}
            public void afterTextChanged(Editable s) {
                if (s.length() == 0 || ws == null) return;
                String txt = s.toString();
                s.clear();
                for (int i = 0; i < txt.length(); i++) {
                    int cp = txt.codePointAt(i);
                    if (Character.isSupplementaryCodePoint(cp)) i++;
                    int ks = (cp >= 0x20 && cp <= 0xff) ? cp : (cp > 0xff ? (0x01000000 | cp) : 0);
                    if (ks != 0) {
                        final int sendKs = ks;
                        ws.sendText("{\"type\":\"key\",\"keysym\":" + sendKs + ",\"pressed\":true}");
                        ui.postDelayed(() -> {
                            if (ws != null) ws.sendText("{\"type\":\"key\",\"keysym\":" + sendKs + ",\"pressed\":false}");
                        }, 25);
                    }
                }
            }
        });
    }

    private void openSoftKeyboard() {
        kbdBridge.requestFocus();
        InputMethodManager imm = (InputMethodManager) getSystemService(Context.INPUT_METHOD_SERVICE);
        if (imm != null) imm.showSoftInput(kbdBridge, InputMethodManager.SHOW_IMPLICIT);
    }

    private Button makeChip(String text, Runnable onClick) {
        Button b = new Button(this);
        b.setText(text);
        b.setTextColor(Color.WHITE);
        b.setTextSize(11);
        b.setAllCaps(false);
        b.setBackground(roundedBg(0xFF1E293B, 10));
        b.setPadding(dp(10), dp(4), dp(10), dp(4));
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.WRAP_CONTENT, dp(34)
        );
        lp.setMargins(0, 0, dp(6), 0);
        b.setLayoutParams(lp);
        b.setOnClickListener(v -> onClick.run());
        return b;
    }

    private Button makeBtn(String text, int color, View.OnClickListener l) {
        Button b = new Button(this);
        b.setText(text);
        b.setTextColor(Color.WHITE);
        b.setTextSize(12);
        b.setAllCaps(false);
        b.setBackground(roundedBg(color, 10));
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(0, dp(40), 1f);
        lp.setMargins(dp(3), 0, dp(3), 0);
        b.setLayoutParams(lp);
        b.setOnClickListener(l);
        return b;
    }

    private EditText makeInput(String hint, String value) {
        EditText et = new EditText(this);
        et.setHint(hint);
        et.setText(value);
        et.setTextColor(Color.WHITE);
        et.setHintTextColor(0xFF64748B);
        et.setTextSize(14);
        et.setSingleLine(true);
        et.setPadding(dp(10), dp(8), dp(10), dp(8));
        et.setBackground(roundedBg(0xFF1E293B, 8));
        return et;
    }

    private Spinner makeSpinner(String[] items, int sel) {
        Spinner s = new Spinner(this);
        ArrayAdapter<String> ad = new ArrayAdapter<>(this, android.R.layout.simple_spinner_item, items);
        ad.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item);
        s.setAdapter(ad);
        s.setSelection(sel);
        return s;
    }

    private LinearLayout wrapSpinner(String label, Spinner s) {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        TextView tv = new TextView(this);
        tv.setText(label);
        tv.setTextColor(0xFF94A3B8);
        tv.setTextSize(11);
        box.addView(tv);
        box.addView(s);
        return box;
    }

    private void togglePanel() {
        panelWrap.setVisibility(panelWrap.getVisibility() == View.VISIBLE ? View.GONE : View.VISIBLE);
    }

    private void enterFullscreen() {
        if (Build.VERSION.SDK_INT >= 30) {
            WindowInsetsController ctrl = getWindow().getInsetsController();
            if (ctrl != null) {
                ctrl.hide(WindowInsets.Type.statusBars() | WindowInsets.Type.navigationBars());
                ctrl.setSystemBarsBehavior(WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
            }
        } else {
            getWindow().getDecorView().setSystemUiVisibility(
                View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                    | View.SYSTEM_UI_FLAG_FULLSCREEN
                    | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                    | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                    | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
            );
        }
    }

    private void acquireWifiLock() {
        try {
            if (wifiLock == null) {
                WifiManager wm = (WifiManager) getApplicationContext().getSystemService(Context.WIFI_SERVICE);
                if (wm != null) {
                    int mode = (Build.VERSION.SDK_INT >= 29)
                        ? WifiManager.WIFI_MODE_FULL_LOW_LATENCY
                        : WifiManager.WIFI_MODE_FULL_HIGH_PERF;
                    wifiLock = wm.createWifiLock(mode, "DeskCastLowLatency");
                    wifiLock.setReferenceCounted(false);
                }
            }
            if (wifiLock != null && !wifiLock.isHeld()) wifiLock.acquire();
        } catch (Exception ignored) {
        }
    }

    private void releaseWifiLock() {
        try {
            if (wifiLock != null && wifiLock.isHeld()) wifiLock.release();
        } catch (Exception ignored) {
        }
    }

    private void connectNow() {
        userDisconnected = false;
        String host = editHost.getText().toString().trim();
        int port = 8765;
        try { port = Integer.parseInt(editPort.getText().toString().trim()); } catch (Exception ignored) {}
        boolean tls = chkHttps.isChecked();
        String pin = editPin.getText().toString().trim();

        getSharedPreferences(PREFS, MODE_PRIVATE).edit()
            .putString("host", host)
            .putString("port", String.valueOf(port))
            .putString("pin", pin)
            .putBoolean("https", tls)
            .putBoolean("trackpad", chkTrackpad.isChecked())
            .apply();

        if (ws != null) {
            ws.close();
            ws = null;
        }
        renderer.reset();
        acquireWifiLock();

        final String fHost = host;
        final int fPort = port;
        final String fPin = pin;

        ui.post(() -> statusBadge.setText("Conectando a " + fHost + ":" + fPort + "..."));

        ws = new WsClient(host, port, tls, new WsClient.Listener() {
            @Override
            public void onOpen() {
                if (ws != null) {
                    ws.sendText("{\"type\":\"caps\",\"webcodecs\":true,\"native\":true}");
                    if (!fPin.isEmpty()) {
                        ws.sendText("{\"type\":\"auth\",\"pin\":\"" + fPin.replace("\"", "") + "\"}");
                    }
                }
                ui.post(() -> panelWrap.setVisibility(View.GONE));
            }

            @Override
            public void onText(String text) {
                try {
                    JSONObject obj = new JSONObject(text);
                    String type = obj.optString("type");
                    if ("pong".equals(type)) {
                        long t = obj.optLong("t", 0);
                        if (t > 0) lastPingMs = Math.max(1, System.currentTimeMillis() - t);
                    } else if ("hello".equals(type)) {
                        int w = obj.optInt("width", streamW);
                        int h = obj.optInt("height", streamH);
                        String codec = obj.optString("codec", "tiles");
                        boolean authed = obj.optBoolean("authed", true);
                        mirrorMode = obj.optBoolean("mirror", false);
                        ui.post(() -> {
                            streamW = w;
                            streamH = h;
                            btnMirror.setText("Espejo: " + (mirrorMode ? "ON" : "OFF"));
                            fitSurfaceAspect();
                        });
                        if (authed && !"h264".equals(codec) && ws != null) {
                            ws.sendText("{\"type\":\"config\",\"codec\":\"h264\"}");
                        }
                    } else if ("auth_ok".equals(type) && ws != null) {
                        ws.sendText("{\"type\":\"config\",\"codec\":\"h264\"}");
                    } else if ("auth_err".equals(type)) {
                        ui.post(() -> {
                            panelWrap.setVisibility(View.VISIBLE);
                            statusBadge.setText("PIN incorrecto");
                        });
                    }
                } catch (Exception ignored) {
                }
            }

            @Override
            public void onBinary(byte[] data) {
                bytesCounter += data.length;
                if (data.length > 6 && data[0] == 0x03) {
                    int w = ((data[1] & 0xFF) << 8) | (data[2] & 0xFF);
                    int h = ((data[3] & 0xFF) << 8) | (data[4] & 0xFF);
                    boolean isKey = data[5] == 1;
                    if (w != streamW || h != streamH) {
                        ui.post(() -> {
                            streamW = w;
                            streamH = h;
                            fitSurfaceAspect();
                        });
                    }
                    if (surfaceReady) {
                        renderer.decodeFrame(w, h, isKey, data, 6, data.length - 6);
                    } else if (isKey) {
                        pendingKeyPacket = data;
                    }
                } else if (data.length > 5 && (data[0] == 0x01 || data[0] == 0x02)) {
                    if (ws != null) ws.sendText("{\"type\":\"ack\"}");
                }
            }

            @Override
            public void onClose(String reason) {
                renderer.reset();
                lastPingMs = 0;
                ui.post(() -> {
                    statusBadge.setText("Reconectando... (" + reason + ")");
                    if (!userDisconnected) {
                        ui.postDelayed(() -> {
                            if (!userDisconnected && !isFinishing()) connectNow();
                        }, 1500);
                    }
                });
            }
        });
        ws.start();
    }

    private void sendConfig() {
        if (ws == null) return;
        try {
            String[] wh = spinRes.getSelectedItem().toString().split("x");
            int w = Integer.parseInt(wh[0]);
            int h = Integer.parseInt(wh[1]);
            String pos = spinPos.getSelectedItem().toString();
            int br = Integer.parseInt(spinBitrate.getSelectedItem().toString());
            ws.sendText(
                "{\"type\":\"config\",\"codec\":\"h264\",\"width\":" + w +
                ",\"height\":" + h + ",\"position\":\"" + pos + "\",\"bitrate\":" + br + "}"
            );
            panelWrap.setVisibility(View.GONE);
        } catch (Exception ignored) {
        }
    }

    private void fitSurfaceAspect() {
        int rw = root.getWidth();
        int rh = root.getHeight();
        if (rw <= 0 || rh <= 0 || streamW <= 0 || streamH <= 0) return;
        double scale = Math.min((double) rw / streamW, (double) rh / streamH);
        int dw = Math.max(1, (int) Math.round(streamW * scale));
        int dh = Math.max(1, (int) Math.round(streamH * scale));
        FrameLayout.LayoutParams lp = (FrameLayout.LayoutParams) surfaceView.getLayoutParams();
        if (lp.width != dw || lp.height != dh) {
            lp.width = dw;
            lp.height = dh;
            lp.gravity = Gravity.CENTER;
            surfaceView.setLayoutParams(lp);
        }
    }

    private boolean handleSurfaceTouch(MotionEvent ev) {
        if (ws == null) return true;
        int vw = Math.max(1, surfaceView.getWidth());
        int vh = Math.max(1, surfaceView.getHeight());
        boolean trackpad = chkTrackpad != null && chkTrackpad.isChecked();

        int action = ev.getActionMasked();
        if (ev.getPointerCount() >= 2) {
            if (action == MotionEvent.ACTION_POINTER_DOWN) {
                scrolling2F = true;
                prevScrollY = ev.getY();
            } else if (action == MotionEvent.ACTION_MOVE && scrolling2F) {
                float dy = (prevScrollY - ev.getY()) * 0.15f;
                prevScrollY = ev.getY();
                ws.sendText("{\"type\":\"pointer\",\"action\":\"scroll\",\"dx\":0,\"dy\":" + dy + "}");
            }
            return true;
        }

        if (trackpad) {
            if (action == MotionEvent.ACTION_DOWN) {
                scrolling2F = false;
                lastTouchX = ev.getX();
                lastTouchY = ev.getY();
                int btn = rightClickArmed ? 2 : 0;
                if (rightClickArmed) {
                    rightClickArmed = false;
                    btnRClick.setText("Clic der.");
                }
                ws.sendText("{\"type\":\"pointer\",\"action\":\"down\",\"x\":" + trackX + ",\"y\":" + trackY + ",\"button\":" + btn + "}");
            } else if (action == MotionEvent.ACTION_MOVE && !scrolling2F) {
                long now = System.currentTimeMillis();
                if (now - lastMoveTs >= 12) {
                    lastMoveTs = now;
                    trackX = Math.max(0.0, Math.min(1.0, trackX + (ev.getX() - lastTouchX) * 1.3 / vw));
                    trackY = Math.max(0.0, Math.min(1.0, trackY + (ev.getY() - lastTouchY) * 1.3 / vh));
                    lastTouchX = ev.getX();
                    lastTouchY = ev.getY();
                    ws.sendText("{\"type\":\"pointer\",\"action\":\"move\",\"x\":" + trackX + ",\"y\":" + trackY + "}");
                }
            } else if (action == MotionEvent.ACTION_UP || action == MotionEvent.ACTION_CANCEL) {
                scrolling2F = false;
                ws.sendText("{\"type\":\"pointer\",\"action\":\"up\",\"x\":" + trackX + ",\"y\":" + trackY + ",\"button\":0}");
            }
            return true;
        }

        double nx = Math.max(0.0, Math.min(1.0, ev.getX() / vw));
        double ny = Math.max(0.0, Math.min(1.0, ev.getY() / vh));
        if (action == MotionEvent.ACTION_DOWN) {
            scrolling2F = false;
            int btn = rightClickArmed ? 2 : 0;
            if (rightClickArmed) {
                rightClickArmed = false;
                btnRClick.setText("Clic der.");
            }
            ws.sendText("{\"type\":\"pointer\",\"action\":\"down\",\"x\":" + nx + ",\"y\":" + ny + ",\"button\":" + btn + "}");
        } else if (action == MotionEvent.ACTION_MOVE && !scrolling2F) {
            long now = System.currentTimeMillis();
            if (now - lastMoveTs >= 12) {
                lastMoveTs = now;
                ws.sendText("{\"type\":\"pointer\",\"action\":\"move\",\"x\":" + nx + ",\"y\":" + ny + "}");
            }
        } else if (action == MotionEvent.ACTION_UP || action == MotionEvent.ACTION_CANCEL) {
            scrolling2F = false;
            ws.sendText("{\"type\":\"pointer\",\"action\":\"up\",\"x\":" + nx + ",\"y\":" + ny + ",\"button\":0}");
        }
        return true;
    }

    @Override
    public void surfaceCreated(SurfaceHolder holder) {
        surfaceReady = true;
        renderer.setSurface(holder.getSurface());
        byte[] pk = pendingKeyPacket;
        pendingKeyPacket = null;
        if (pk != null && pk.length > 6) {
            int w = ((pk[1] & 0xFF) << 8) | (pk[2] & 0xFF);
            int h = ((pk[3] & 0xFF) << 8) | (pk[4] & 0xFF);
            renderer.decodeFrame(w, h, pk[5] == 1, pk, 6, pk.length - 6);
        }
        if (ws != null) ws.sendText("{\"type\":\"keyframe\"}");
    }

    @Override
    public void surfaceChanged(SurfaceHolder holder, int format, int width, int height) {
        surfaceReady = true;
        renderer.setSurface(holder.getSurface());
    }

    @Override
    public void surfaceDestroyed(SurfaceHolder holder) {
        surfaceReady = false;
        renderer.setSurface(null);
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) enterFullscreen();
    }

    @Override
    protected void onDestroy() {
        userDisconnected = true;
        ui.removeCallbacks(statsTick);
        if (ws != null) ws.close();
        renderer.release();
        releaseWifiLock();
        super.onDestroy();
    }
}
