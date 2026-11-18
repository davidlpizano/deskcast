(() => {
  const canvas = document.getElementById('screen');
  const ctx = canvas.getContext('2d', { alpha: false });
  const banner = document.getElementById('status-banner');
  const fab = document.getElementById('fab-toggle');
  const hud = document.getElementById('hud-panel');
  const hudRes = document.getElementById('hud-res');
  const hudFps = document.getElementById('hud-fps');
  const optInput = document.getElementById('opt-input');
  const optPos = document.getElementById('opt-pos');
  const optRes = document.getElementById('opt-res');
  const optQ = document.getElementById('opt-q');
  const lblQ = document.getElementById('lbl-q');
  const optCodec = document.getElementById('opt-codec');
  const btnFull = document.getElementById('btn-full');
  const btnKbd = document.getElementById('btn-kbd');
  const btnRClick = document.getElementById('btn-rclick');
  const btnMirror = document.getElementById('btn-mirror');
  const btnCloseHud = document.getElementById('btn-close-hud');
  const kbdBridge = document.getElementById('kbd-bridge');
  const pinModal = document.getElementById('pin-modal');
  const pinInput = document.getElementById('pin-input');
  const pinErr = document.getElementById('pin-err');
  const pinSubmit = document.getElementById('pin-submit');

  let ws = null;
  let state = { width: 1280, height: 800, mirror: false, position: 'right', quality: 58, codec: 'tiles' };
  let fpsCount = 0;
  let bytesInSec = 0;
  let decoding = false;
  let packetQueue = [];
  let wakeLock = null;
  let rightClickArmed = false;
  let h264Decoder = null;
  let h264Codec = '';
  let h264W = 0;
  let h264H = 0;
  let h264NeedKey = true;
  let h264Ts = 0;

  function showBanner(msg, autoHideMs = 0) {
    banner.textContent = msg;
    banner.classList.remove('is-hidden');
    if (autoHideMs > 0) {
      setTimeout(() => banner.classList.add('is-hidden'), autoHideMs);
    }
  }

  let fadeTimer = null;
  function bumpFab() {
    fab.classList.remove('is-faded');
    clearTimeout(fadeTimer);
    fadeTimer = setTimeout(() => {
      if (hud.classList.contains('is-hidden')) fab.classList.add('is-faded');
    }, 3000);
  }
  bumpFab();

  fab.addEventListener('click', () => {
    hud.classList.toggle('is-hidden');
    bumpFab();
  });
  btnCloseHud.addEventListener('click', () => {
    hud.classList.add('is-hidden');
    bumpFab();
  });

  async function requestWakeLock() {
    try {
      if ('wakeLock' in navigator && !wakeLock) {
        wakeLock = await navigator.wakeLock.request('screen');
        wakeLock.addEventListener('release', () => { wakeLock = null; });
      }
    } catch (_) {}
  }

  btnFull.addEventListener('click', async () => {
    try {
      if (!document.fullscreenElement) {
        await document.documentElement.requestFullscreen();
        if (screen.orientation && screen.orientation.lock) {
          screen.orientation.lock('landscape').catch(() => {});
        }
      } else {
        await document.exitFullscreen();
      }
    } catch (_) {}
    requestWakeLock();
    hud.classList.add('is-hidden');
  });

  function sendJson(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(obj));
    }
  }

  function extractAvcCodec(nal) {
    for (let i = 0; i < nal.length - 7; i++) {
      if (nal[i] === 0 && nal[i + 1] === 0) {
        let start = -1;
        if (nal[i + 2] === 1) start = i + 3;
        else if (nal[i + 2] === 0 && nal[i + 3] === 1) start = i + 4;
        if (start > 0 && start + 3 < nal.length && (nal[start] & 0x1f) === 7) {
          const p = nal[start + 1].toString(16).padStart(2, '0');
          const c = nal[start + 2].toString(16).padStart(2, '0');
          const l = nal[start + 3].toString(16).padStart(2, '0');
          return `avc1.${p}${c}${l}`;
        }
      }
    }
    return 'avc1.42c020';
  }

  function resetH264() {
    if (h264Decoder) {
      try { h264Decoder.close(); } catch (_) {}
      h264Decoder = null;
    }
    h264Codec = '';
    h264NeedKey = true;
  }

  function handleH264Packet(buf) {
    if (typeof VideoDecoder === 'undefined') {
      showBanner('H.264 requiere USB (127.0.0.1) o HTTPS; cambiando a Turbo Tiles...', 3000);
      sendJson({ type: 'config', codec: 'tiles' });
      return;
    }
    const dv = new DataView(buf.buffer, buf.byteOffset, buf.byteLength);
    const w = dv.getUint16(1);
    const h = dv.getUint16(3);
    const isKey = buf[5] === 1;
    const nal = buf.subarray(6);

    if (h264NeedKey && !isKey) {
      return;
    }

    if (isKey) {
      const codecStr = extractAvcCodec(nal);
      if (!h264Decoder || h264Decoder.state === 'closed' || h264Codec !== codecStr || h264W !== w || h264H !== h) {
        resetH264();
        h264Codec = codecStr;
        h264W = w;
        h264H = h;
        h264Decoder = new VideoDecoder({
          output: frame => {
            if (canvas.width !== w || canvas.height !== h) {
              canvas.width = w;
              canvas.height = h;
              hudRes.textContent = `${w}×${h}`;
            }
            ctx.drawImage(frame, 0, 0);
            frame.close();
            fpsCount++;
          },
          error: () => {
            resetH264();
            sendJson({ type: 'keyframe' });
          },
        });
        h264Decoder.configure({ codec: codecStr, optimizeForLatency: true });
      }
      h264NeedKey = false;
    }

    if (!h264Decoder || h264Decoder.state !== 'configured') return;
    if (h264Decoder.decodeQueueSize > 4 && !isKey) return;
    try {
      h264Ts += 16666;
      h264Decoder.decode(new EncodedVideoChunk({
        type: isKey ? 'key' : 'delta',
        timestamp: h264Ts,
        data: nal,
      }));
    } catch (_) {
      resetH264();
      sendJson({ type: 'keyframe' });
    }
  }

  async function processQueue() {
    if (decoding || packetQueue.length === 0) return;
    decoding = true;
    try {
      while (packetQueue.length > 0) {
        // Si llega un keyframe completo, descartar deltas anteriores
        const lastFull = packetQueue.findLastIndex ? packetQueue.findLastIndex(b => b[0] === 0x01) : -1;
        if (lastFull > 0) packetQueue.splice(0, lastFull);
        const buf = packetQueue.shift();
        const kind = buf[0];
        const dv = new DataView(buf.buffer, buf.byteOffset, buf.byteLength);
        const w = dv.getUint16(1);
        const h = dv.getUint16(3);
        if (canvas.width !== w || canvas.height !== h) {
          canvas.width = w;
          canvas.height = h;
          hudRes.textContent = `${w}×${h}`;
        }

        if (kind === 0x01) {
          const blob = new Blob([buf.subarray(5)], { type: 'image/jpeg' });
          const bmp = await createImageBitmap(blob);
          ctx.drawImage(bmp, 0, 0);
          bmp.close();
          fpsCount++;
        } else if (kind === 0x02) {
          const count = dv.getUint16(5);
          let off = 7;
          const jobs = [];
          for (let i = 0; i < count; i++) {
            const tx = dv.getUint16(off);
            const ty = dv.getUint16(off + 2);
            const tlen = dv.getUint32(off + 4);
            off += 8;
            const blob = new Blob([buf.subarray(off, off + tlen)], { type: 'image/jpeg' });
            off += tlen;
            jobs.push(createImageBitmap(blob).then(bmp => ({ tx, ty, bmp })));
          }
          const bitmaps = await Promise.all(jobs);
          for (const item of bitmaps) {
            ctx.drawImage(item.bmp, item.tx, item.ty);
            item.bmp.close();
          }
          fpsCount++;
        }
        sendJson({ type: 'ack' });
      }
    } catch (_) {
    } finally {
      decoding = false;
      if (packetQueue.length > 0) processQueue();
    }
  }

  let lastPingMs = 0;
  setInterval(() => {
    const kbps = Math.round((bytesInSec * 8) / 1000);
    const lat = lastPingMs > 0 ? ` · ${lastPingMs}ms` : '';
    hudFps.textContent = `${fpsCount} FPS · ${kbps > 1000 ? (kbps / 1000).toFixed(1) + ' Mbps' : kbps + ' kbps'}${lat}`;
    fpsCount = 0;
    bytesInSec = 0;
    sendJson({ type: 'ping', t: Date.now() });
  }, 1000);

  function connect() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(`${proto}//${location.host}/ws`);
    ws.binaryType = 'arraybuffer';

    ws.onopen = () => {
      showBanner('Conectado · Esperando imagen...', 1600);
      sendJson({ type: 'caps', webcodecs: typeof VideoDecoder !== 'undefined' });
      requestWakeLock();
    };

    ws.onmessage = ev => {
      if (typeof ev.data === 'string') {
        try {
          const msg = JSON.parse(ev.data);
          if (msg.type === 'hello') {
            state = { ...state, ...msg };
            hudRes.textContent = `${msg.width}×${msg.height}`;
            optPos.value = msg.position || 'right';
            optQ.value = msg.quality || 58;
            lblQ.textContent = optQ.value;
            if (optCodec && msg.codec) optCodec.value = msg.codec;
            btnMirror.textContent = `Espejo: ${msg.mirror ? 'ON' : 'OFF'}`;
            if (msg.codec !== 'h264') resetH264();
            if (msg.needPin && !msg.authed) {
              pinModal.classList.remove('is-hidden');
              pinInput.focus();
            } else {
              pinModal.classList.add('is-hidden');
            }
          } else if (msg.type === 'pong' && msg.t) {
            lastPingMs = Math.max(1, Date.now() - msg.t);
          } else if (msg.type === 'auth_ok') {
            pinErr.textContent = '';
            pinModal.classList.add('is-hidden');
          } else if (msg.type === 'auth_err') {
            pinErr.textContent = 'PIN incorrecto';
          }
        } catch (_) {}
        return;
      }

      const buf = new Uint8Array(ev.data);
      bytesInSec += buf.byteLength;
      if (buf.length > 6 && buf[0] === 0x03) {
        handleH264Packet(buf);
      } else if (buf.length > 5 && (buf[0] === 0x01 || buf[0] === 0x02)) {
        packetQueue.push(buf);
        processQueue();
      }
    };

    ws.onclose = () => {
      packetQueue = [];
      resetH264();
      showBanner('Reconectando al PC Linux...');
      setTimeout(connect, 1500);
    };
  }

  pinSubmit.addEventListener('click', () => sendJson({ type: 'auth', pin: pinInput.value }));
  pinInput.addEventListener('keydown', e => {
    if (e.key === 'Enter') sendJson({ type: 'auth', pin: pinInput.value });
  });

  optPos.addEventListener('change', () => sendJson({ type: 'config', position: optPos.value }));
  optQ.addEventListener('input', () => { lblQ.textContent = optQ.value; });
  optQ.addEventListener('change', () => sendJson({ type: 'config', quality: parseInt(optQ.value, 10) }));
  if (optCodec) {
    optCodec.addEventListener('change', () => sendJson({ type: 'config', codec: optCodec.value }));
  }
  btnMirror.addEventListener('click', () => sendJson({ type: 'config', mirror: !state.mirror }));
  optRes.addEventListener('change', () => {
    let w, h;
    if (optRes.value === 'auto') {
      const sw = Math.max(window.innerWidth, window.innerHeight);
      const sh = Math.min(window.innerWidth, window.innerHeight);
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      w = Math.round(sw * dpr / 8) * 8;
      h = Math.round(sh * dpr / 8) * 8;
    } else {
      [w, h] = optRes.value.split('x').map(Number);
    }
    sendJson({ type: 'config', width: w, height: h });
  });

  btnRClick.addEventListener('click', () => {
    rightClickArmed = !rightClickArmed;
    btnRClick.classList.toggle('btn-pri', rightClickArmed);
    btnRClick.textContent = rightClickArmed ? 'Clic derecho (listo)' : 'Clic derecho';
  });

  function canvasNormCoords(clientX, clientY) {
    const r = canvas.getBoundingClientRect();
    const cw = canvas.width || state.width;
    const ch = canvas.height || state.height;
    const scale = Math.min(r.width / cw, r.height / ch);
    const drawW = cw * scale;
    const drawH = ch * scale;
    const offX = r.left + (r.width - drawW) / 2;
    const offY = r.top + (r.height - drawH) / 2;
    return {
      x: Math.max(0, Math.min(1, (clientX - offX) / drawW)),
      y: Math.max(0, Math.min(1, (clientY - offY) / drawH)),
    };
  }

  let ptrDown = false;
  let trackX = 0.5;
  let trackY = 0.5;
  let lastClientX = 0;
  let lastClientY = 0;

  canvas.addEventListener('contextmenu', e => e.preventDefault());

  canvas.addEventListener('pointerdown', e => {
    requestWakeLock();
    if (optInput.value === 'view') return;
    canvas.setPointerCapture(e.pointerId);
    ptrDown = true;
    lastClientX = e.clientX;
    lastClientY = e.clientY;
    const btn = rightClickArmed ? 2 : e.button;
    if (rightClickArmed) {
      rightClickArmed = false;
      btnRClick.classList.remove('btn-pri');
      btnRClick.textContent = 'Clic derecho';
    }
    if (optInput.value === 'trackpad') {
      sendJson({ type: 'pointer', action: 'down', x: trackX, y: trackY, button: btn });
    } else {
      const p = canvasNormCoords(e.clientX, e.clientY);
      sendJson({ type: 'pointer', action: 'down', x: p.x, y: p.y, button: btn });
    }
  });

  let lastMoveSend = 0;
  canvas.addEventListener('pointermove', e => {
    if (optInput.value === 'view') return;
    const now = performance.now();
    if (now - lastMoveSend < 16) return;
    lastMoveSend = now;
    if (optInput.value === 'trackpad') {
      if (!ptrDown) return;
      const dx = (e.clientX - lastClientX) / Math.max(200, canvas.clientWidth);
      const dy = (e.clientY - lastClientY) / Math.max(200, canvas.clientHeight);
      lastClientX = e.clientX;
      lastClientY = e.clientY;
      trackX = Math.max(0, Math.min(1, trackX + dx * 1.3));
      trackY = Math.max(0, Math.min(1, trackY + dy * 1.3));
      sendJson({ type: 'pointer', action: 'move', x: trackX, y: trackY });
    } else {
      if (e.pointerType === 'touch' && !ptrDown) return;
      const p = canvasNormCoords(e.clientX, e.clientY);
      sendJson({ type: 'pointer', action: 'move', x: p.x, y: p.y });
    }
  });

  function onPtrUp(e) {
    if (!ptrDown) return;
    ptrDown = false;
    if (optInput.value === 'view') return;
    if (optInput.value === 'trackpad') {
      sendJson({ type: 'pointer', action: 'up', x: trackX, y: trackY, button: e.button });
    } else {
      const p = canvasNormCoords(e.clientX, e.clientY);
      sendJson({ type: 'pointer', action: 'up', x: p.x, y: p.y, button: e.button });
    }
  }
  canvas.addEventListener('pointerup', onPtrUp);
  canvas.addEventListener('pointercancel', onPtrUp);

  canvas.addEventListener('wheel', e => {
    if (optInput.value === 'view') return;
    e.preventDefault();
    sendJson({ type: 'pointer', action: 'scroll', dx: e.deltaX * 0.1, dy: e.deltaY * 0.1 });
  }, { passive: false });

  const SPECIAL_KEYSYMS = {
    Backspace: 0xff08, Tab: 0xff09, Enter: 0xff0d, Escape: 0xff1b,
    ArrowLeft: 0xff51, ArrowUp: 0xff52, ArrowRight: 0xff53, ArrowDown: 0xff54,
    Delete: 0xffff, Home: 0xff50, End: 0xff57,
  };
  function sendKeysym(ks) {
    sendJson({ type: 'key', keysym: ks, pressed: true });
    setTimeout(() => sendJson({ type: 'key', keysym: ks, pressed: false }), 25);
  }
  btnKbd.addEventListener('click', () => kbdBridge.focus());
  kbdBridge.addEventListener('keydown', e => {
    if (SPECIAL_KEYSYMS[e.key]) {
      e.preventDefault();
      sendKeysym(SPECIAL_KEYSYMS[e.key]);
    }
  });
  kbdBridge.addEventListener('input', () => {
    const val = kbdBridge.value;
    kbdBridge.value = '';
    for (const ch of val) {
      const cp = ch.codePointAt(0);
      if (cp >= 0x20 && cp <= 0xff) {
        sendKeysym(cp);
      } else if (cp > 0xff) {
        sendKeysym(0x01000000 | cp);
      }
    }
  });

  connect();
})();
