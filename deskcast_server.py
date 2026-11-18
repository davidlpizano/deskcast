#!/usr/bin/env python3
"""Servidor DeskCast: Monitor virtual GNOME Wayland con Dirty-Tile Diff en C + H.264."""
import argparse
import asyncio
import base64
import ctypes
import hashlib
import io
import json
import mimetypes
import os
import signal
import socket
import ssl
import struct
import subprocess
import threading
import time

from concurrent.futures import ThreadPoolExecutor
import gi
from PIL import Image

gi.require_version('Gst', '1.0')
from gi.repository import GLib, Gst  # noqa: E402

try:
    gi.require_version('GstVideo', '1.0')
    from gi.repository import GstVideo  # noqa: E402
except Exception:
    GstVideo = None

from mutter_display import MutterVirtualMonitor  # noqa: E402
from red_remota import detectar_redes  # noqa: E402

Gst.init(None)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, 'web')
WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
TILE_SZ = 128
MAX_IN_FLIGHT = 3


def _encode_tiles_packet(rgb: bytes, w: int, h: int, full_frame: bool, tiles_list, quality: int) -> bytes:
    """Codifica un keyframe completo (0x01) o un lote de tiles sucios (0x02) en un hilo trabajador."""
    im = Image.frombuffer('RGB', (w, h), rgb, 'raw', 'RGB', 0, 1)
    if full_frame:
        buf = io.BytesIO()
        im.save(buf, format='JPEG', quality=quality, subsampling=1)
        return struct.pack('!BHH', 0x01, w, h) + buf.getvalue()

    cols = (w + TILE_SZ - 1) // TILE_SZ
    out = bytearray(struct.pack('!BHHH', 0x02, w, h, len(tiles_list)))
    for idx in tiles_list:
        ty, tx = divmod(idx, cols)
        x0 = tx * TILE_SZ
        y0 = ty * TILE_SZ
        x1 = min(w, x0 + TILE_SZ)
        y1 = min(h, y0 + TILE_SZ)
        if x0 >= w or y0 >= h:
            continue
        tbuf = io.BytesIO()
        im.crop((x0, y0, x1, y1)).save(tbuf, format='JPEG', quality=quality, subsampling=1)
        jb = tbuf.getvalue()
        out.extend(struct.pack('!HHI', x0, y0, len(jb)))
        out.extend(jb)
    return bytes(out)


def _load_diff_lib():
    so_path = os.path.join(BASE_DIR, 'fast_diff.so')
    c_path = os.path.join(BASE_DIR, 'fast_diff.c')
    if not os.path.isfile(so_path) and os.path.isfile(c_path):
        subprocess.run(['gcc', '-O3', '-shared', '-fPIC', c_path, '-o', so_path], check=False)
    if os.path.isfile(so_path):
        lib = ctypes.CDLL(so_path)
        lib.diff_tiles_rgb.argtypes = [
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_uint8),
        ]
        lib.diff_tiles_rgb.restype = ctypes.c_int
        return lib
    return None


DIFF_LIB = _load_diff_lib()


def ensure_self_signed_cert():
    cfg_dir = os.path.expanduser('~/.config/deskcast')
    os.makedirs(cfg_dir, exist_ok=True)
    cert = os.path.join(cfg_dir, 'cert.pem')
    key = os.path.join(cfg_dir, 'key.pem')
    if not (os.path.isfile(cert) and os.path.isfile(key)):
        subprocess.run([
            'openssl', 'req', '-x509', '-newkey', 'rsa:2048',
            '-keyout', key, '-out', cert, '-days', '365', '-nodes',
            '-subj', '/CN=deskcast',
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return cert, key


class ClientSession:
    def __init__(self, reader, writer, peer, need_pin):
        self.reader = reader
        self.writer = writer
        self.peer = peer
        self.authed = not need_pin
        self.closed = False
        self.in_flight = 0
        self.encoding = False
        self.needs_keyframe = True
        self.has_webcodecs = True
        self.pending_tiles = set()
        self.last_send_ts = 0.0
        self.frames_sent = 0
        self.bytes_sent = 0

    async def send_ws_frame(self, opcode, payload: bytes):
        if self.closed:
            return
        n = len(payload)
        header = bytearray([0x80 | opcode])
        if n < 126:
            header.append(n)
        elif n < 65536:
            header.append(126)
            header.extend(struct.pack('!H', n))
        else:
            header.append(127)
            header.extend(struct.pack('!Q', n))
        try:
            self.writer.write(bytes(header) + payload)
            await self.writer.drain()
        except Exception:
            self.closed = True

    def send_binary_nowait(self, packet: bytes, track_inflight=True):
        if self.closed or not self.authed:
            return False
        if self.writer.transport.get_write_buffer_size() > 262144:
            return False
        n = len(packet)
        header = bytearray([0x82])
        if n < 126:
            header.append(n)
        elif n < 65536:
            header.append(126)
            header.extend(struct.pack('!H', n))
        else:
            header.append(127)
            header.extend(struct.pack('!Q', n))
        try:
            self.writer.write(bytes(header) + packet)
            if track_inflight:
                self.in_flight += 1
            self.last_send_ts = time.monotonic()
            self.frames_sent += 1
            self.bytes_sent += n
            return True
        except Exception:
            self.closed = True
            return False


class DeskCastServer:
    """Controlador principal del monitor virtual, captura diferencial C/JPEG o H.264 y HTTP/WS."""

    def __init__(self, host='0.0.0.0', port=8765, width=1280, height=800,
                 fps=30, quality=58, position='right', mirror=False, pin='',
                 codec='tiles', use_https=False, bitrate=4000, keep_fps=True):
        self.host = host
        self.port = int(port)
        self.width = int(width)
        self.height = int(height)
        self.fps = max(5, min(60, int(fps)))
        self.quality = max(25, min(95, int(quality)))
        self.position = position
        self.mirror = bool(mirror)
        self.pin = str(pin or '').strip()
        self.codec = codec  # 'tiles' (RGB->Dirty Tiles JPEG) o 'h264'
        self.use_https = bool(use_https)
        self.bitrate = max(500, min(15000, int(bitrate)))
        self.keep_fps = bool(keep_fps)

        self.monitor = None
        self.pipeline = None
        self.enc_pipeline = None
        self.enc_src = None
        self.x264enc = None
        self._cap_lock = threading.Lock()
        self._cap_event = threading.Event()
        self._latest_i420 = None
        self._pacer_running = False
        self._pacer_thread = None
        self.glib_loop = None
        self.glib_thread = None
        self.aio_loop = None
        self.http_server = None

        self.clients = set()
        self._prev_rgb = None
        self._cur_rgb = None
        self._cur_w = self.width
        self._cur_h = self.height
        self._frame_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._enc_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='sd-jpeg')
        self._last_h264_kf = None
        self._last_emit_ts = 0.0
        self._last_motion_ts = 0.0
        self._sharp_pending = False
        self._fps_counter = 0
        self._fps_ts = time.monotonic()
        self.actual_fps = 0
        self.kbps = 0
        self._bytes_window = 0
        self.running = False
        self.status_text = 'Detenido'
        self.on_status_change = None

    def _roll_stats(self, force=False):
        now = time.monotonic()
        with self._stats_lock:
            dt = now - self._fps_ts
            if dt >= 1.0 or (force and dt > 0.2):
                self.actual_fps = round(self._fps_counter / max(dt, 0.001))
                self.kbps = int((self._bytes_window * 8) / (1000 * max(dt, 0.001)))
                self._fps_counter = 0
                self._bytes_window = 0
                self._fps_ts = now

    def _notify_status(self, msg):
        self.status_text = msg
        if self.on_status_change:
            try:
                self.on_status_change(msg)
            except Exception:
                pass

    def start_background(self):
        if self.running:
            return
        self.running = True
        self._start_glib()
        t = threading.Thread(target=self._run_asyncio_thread, daemon=True)
        t.start()

    def stop(self):
        self.running = False
        self._stop_display()
        if self.aio_loop and self.http_server:
            try:
                self.aio_loop.call_soon_threadsafe(self.http_server.close)
            except Exception:
                pass
        if self.aio_loop:
            try:
                self.aio_loop.call_soon_threadsafe(self.aio_loop.stop)
            except Exception:
                pass
        if self.glib_loop and self.glib_loop.is_running():
            try:
                self.glib_loop.quit()
            except Exception:
                pass
        self._notify_status('Detenido')

    def _start_glib(self):
        if self.glib_loop and self.glib_loop.is_running():
            return
        self.glib_loop = GLib.MainLoop()
        self.glib_thread = threading.Thread(target=self.glib_loop.run, daemon=True)
        self.glib_thread.start()
        GLib.idle_add(self._start_display)

    def _stop_display(self):
        self._pacer_running = False
        self._cap_event.set()
        if self._pacer_thread and self._pacer_thread.is_alive():
            self._pacer_thread.join(timeout=1.0)
        self._pacer_thread = None
        if self.enc_pipeline:
            try:
                self.enc_pipeline.set_state(Gst.State.NULL)
            except Exception:
                pass
            self.enc_pipeline = None
            self.enc_src = None
        if self.pipeline:
            try:
                self.pipeline.set_state(Gst.State.NULL)
            except Exception:
                pass
            self.pipeline = None
            self.x264enc = None
        with self._cap_lock:
            self._latest_i420 = None
        if self.monitor:
            try:
                self.monitor.stop()
            except Exception:
                pass
            self.monitor = None
        with self._frame_lock:
            self._prev_rgb = None
            self._cur_rgb = None
            self._last_h264_kf = None

    def _start_display(self):
        was_active = self.monitor is not None
        self._stop_display()
        self._notify_status(f'Creando monitor virtual {self.width}×{self.height}...')
        self.monitor = MutterVirtualMonitor(
            width=self.width, height=self.height,
            position=self.position, mirror=self.mirror,
        )

        def _on_node(node_id):
            self._launch_pipeline(node_id)
            if not self.mirror:
                GLib.timeout_add(700, self._arrange_once)

        def _on_closed():
            if self.running:
                self._notify_status('Sesión Mutter cerrada; pulsa Iniciar de nuevo')

        try:
            self.monitor.start(_on_node, on_closed=_on_closed, wait_clear=was_active)
        except Exception as exc:
            self._notify_status(f'Error Mutter D-Bus: {exc}')
        return False

    def _arrange_once(self):
        if self.monitor and self.monitor.running:
            self.monitor.arrange_virtual_monitor(self.position)
        return False

    def _force_h264_keyframe(self):
        target = self.enc_pipeline or self.pipeline
        if not target or GstVideo is None:
            return False
        try:
            ev = GstVideo.video_event_new_downstream_force_key_unit(
                Gst.CLOCK_TIME_NONE, Gst.CLOCK_TIME_NONE, Gst.CLOCK_TIME_NONE, True, 0
            )
            target.send_event(ev)
        except Exception:
            pass
        return False

    def _ensure_h264_encoder(self, w: int, h: int):
        if self.enc_pipeline and getattr(self, '_enc_wh', None) == (w, h):
            return
        if self.enc_pipeline:
            try:
                self.enc_pipeline.set_state(Gst.State.NULL)
            except Exception:
                pass
            self.enc_pipeline = None
            self.enc_src = None
            self.x264enc = None

        p_enc = (
            f'appsrc name=enc_src is-live=true format=time '
            f'caps=video/x-raw,format=I420,width={w},height={h},framerate={self.fps}/1 ! '
            f'x264enc name=venc tune=zerolatency speed-preset=ultrafast bitrate={self.bitrate} '
            f'vbv-buf-capacity=300 qp-min=10 qp-max=36 '
            f'key-int-max=300 bframes=0 threads=4 sliced-threads=true rc-lookahead=0 ! '
            f'h264parse config-interval=-1 ! '
            f'video/x-h264,stream-format=byte-stream,alignment=au,profile=baseline ! '
            f'appsink name=sink emit-signals=true max-buffers=2 drop=true sync=false'
        )
        ep = Gst.parse_launch(p_enc)
        self.enc_src = ep.get_by_name('enc_src')
        self.x264enc = ep.get_by_name('venc')
        sink = ep.get_by_name('sink')
        sink.connect('new-sample', self._on_gst_sample)
        gbus = ep.get_bus()
        gbus.add_signal_watch()
        gbus.connect('message::error', self._on_gst_error)
        ep.set_state(Gst.State.PLAYING)
        self.enc_pipeline = ep
        self._enc_wh = (w, h)

    def _on_capture_sample(self, sink):
        sample = sink.emit('pull-sample')
        if not sample:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        st = sample.get_caps().get_structure(0)
        ok_w, cw = st.get_int('width')
        ok_h, ch = st.get_int('height')
        w = cw if ok_w else self.width
        h = ch if ok_h else self.height
        with self._cap_lock:
            self._latest_i420 = (buf, w, h, True)
        self._cap_event.set()
        return Gst.FlowReturn.OK

    def _h264_pacer_loop(self):
        t0 = time.monotonic()
        last_push = 0.0
        next_tick = t0
        while self._pacer_running and self.running and self.codec == 'h264':
            interval = 1.0 / max(1, self.fps)
            now = time.monotonic()
            wait_s = max(0.001, next_tick - now)
            self._cap_event.wait(timeout=wait_s)
            if not (self._pacer_running and self.running):
                break
            now = time.monotonic()
            with self._cap_lock:
                item = self._latest_i420
                if item is None:
                    self._cap_event.clear()
                    next_tick = now + interval
                    continue
                buf, w, h, is_fresh = item
                if is_fresh:
                    # Evita ráfagas de <0.45*interval que generan micro-tirones
                    if (now - last_push) < interval * 0.45:
                        self._cap_event.clear()
                        continue
                    self._latest_i420 = (buf, w, h, False)
                    self._cap_event.clear()

            if not is_fresh and not self.keep_fps:
                next_tick = now + interval
                continue
            if not is_fresh and now < next_tick - 0.001:
                continue

            try:
                self._ensure_h264_encoder(w, h)
                push_buf = buf.copy_region(Gst.BufferCopyFlags.MEMORY, 0, buf.get_size())
                push_buf.pts = int((now - t0) * Gst.SECOND)
                push_buf.duration = int(interval * Gst.SECOND)
                self.enc_src.emit('push-buffer', push_buf)
                last_push = now
                next_tick = now + interval
            except Exception as exc:
                self._notify_status(f'Error encoder H.264: {exc}')
                time.sleep(0.1)

    def _launch_pipeline(self, node_id):
        caps = f'video/x-raw,width={self.width},height={self.height}' if not self.mirror else 'video/x-raw'
        if self.codec == 'h264':
            p_str = (
                f'pipewiresrc path={node_id} do-timestamp=true ! '
                f'{caps} ! videoconvert n-threads=4 ! video/x-raw,format=I420 ! '
                f'appsink name=cap_sink emit-signals=true max-buffers=1 drop=true sync=false'
            )
            try:
                pipe = Gst.parse_launch(p_str)
            except Exception as exc:
                self._notify_status(f'Error GStreamer: {exc}')
                return
            cap_sink = pipe.get_by_name('cap_sink')
            cap_sink.connect('new-sample', self._on_capture_sample)
            gbus = pipe.get_bus()
            gbus.add_signal_watch()
            gbus.connect('message::error', self._on_gst_error)
            pipe.set_state(Gst.State.PLAYING)
            self.pipeline = pipe
            self._pacer_running = True
            self._cap_event.clear()
            self._pacer_thread = threading.Thread(target=self._h264_pacer_loop, daemon=True)
            self._pacer_thread.start()
        else:
            p_str = (
                f'pipewiresrc path={node_id} do-timestamp=true ! '
                f'{caps} ! videoconvert n-threads=4 ! video/x-raw,format=RGB ! '
                f'appsink name=sink emit-signals=true max-buffers=1 drop=true sync=false'
            )
            try:
                pipe = Gst.parse_launch(p_str)
            except Exception as exc:
                self._notify_status(f'Error GStreamer: {exc}')
                return
            self.x264enc = None
            sink = pipe.get_by_name('sink')
            sink.connect('new-sample', self._on_gst_sample)
            gbus = pipe.get_bus()
            gbus.add_signal_watch()
            gbus.connect('message::error', self._on_gst_error)
            pipe.set_state(Gst.State.PLAYING)
            self.pipeline = pipe

        modo_txt = 'Espejo' if self.mirror else f'2º Monitor ({self.position})'
        enc_txt = 'H.264' if self.codec == 'h264' else 'Turbo Tiles'
        self._notify_status(f'Activo · {modo_txt} · {self.width}×{self.height} · {enc_txt}')

    def _on_gst_error(self, _bus, msg):
        err, _dbg = msg.parse_error()
        self._notify_status(f'Error pipeline: {err}')

    def _on_gst_sample(self, sink):
        sample = sink.emit('pull-sample')
        if not sample:
            return Gst.FlowReturn.OK

        now = time.monotonic()
        buf = sample.get_buffer()
        is_key = not buf.has_flags(Gst.BufferFlags.DELTA_UNIT)
        ok, map_info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK

        try:
            st = sample.get_caps().get_structure(0)
            ok_w, cw = st.get_int('width')
            ok_h, ch = st.get_int('height')
            w = cw if ok_w else self.width
            h = ch if ok_h else self.height
            cur_buf = bytearray(map_info.data)
        finally:
            buf.unmap(map_info)

        if self.codec == 'h264':
            pkt = struct.pack('!BHHB', 0x03, w, h, 1 if is_key else 0) + bytes(cur_buf)
            if is_key:
                self._last_h264_kf = pkt
            with self._stats_lock:
                self._fps_counter += 1
            self._roll_stats()
            if self.aio_loop and self.clients:
                self.aio_loop.call_soon_threadsafe(self._broadcast_h264, pkt, is_key)
            return Gst.FlowReturn.OK

        # Modo Turbo Tiles (RGB24 -> Dirty Tiles Diff en C sin copias extra)
        min_dt = 1.0 / max(1, self.fps)
        if now - self._last_emit_ts < min_dt * 0.85:
            return Gst.FlowReturn.OK
        self._last_emit_ts = now

        expected_len = w * h * 3
        if len(cur_buf) != expected_len:
            return Gst.FlowReturn.OK

        with self._frame_lock:
            cols = (w + TILE_SZ - 1) // TILE_SZ
            rows = (h + TILE_SZ - 1) // TILE_SZ
            if (
                DIFF_LIB is not None
                and self._prev_rgb is not None
                and len(self._prev_rgb) == expected_len
                and self._cur_w == w
                and self._cur_h == h
            ):
                flags = (ctypes.c_uint8 * (cols * rows))()
                c_cur = (ctypes.c_uint8 * expected_len).from_buffer(cur_buf)
                c_prev = (ctypes.c_uint8 * expected_len).from_buffer(self._prev_rgb)
                cnt = DIFF_LIB.diff_tiles_rgb(c_cur, c_prev, w, h, TILE_SZ, flags)
                del c_cur, c_prev
                if cnt == 0:
                    return Gst.FlowReturn.OK
                dirty_set = {i for i in range(cols * rows) if flags[i]}
            else:
                dirty_set = set(range(cols * rows))

            self._prev_rgb = cur_buf
            self._cur_rgb = bytes(cur_buf)
            self._cur_w = w
            self._cur_h = h

        self._last_motion_ts = now
        self._sharp_pending = True
        with self._stats_lock:
            self._fps_counter += 1
        self._roll_stats()

        if self.aio_loop and self.clients:
            self.aio_loop.call_soon_threadsafe(self._on_new_dirty_tiles, dirty_set, w, h)
        return Gst.FlowReturn.OK

    def _broadcast_h264(self, pkt: bytes, is_key: bool):
        dead = []
        for cli in list(self.clients):
            if cli.closed:
                dead.append(cli)
            elif not cli.authed or not cli.has_webcodecs:
                continue
            elif not is_key and cli.writer.transport.get_write_buffer_size() > 131072:
                # Si el enlace está saturado, descartamos P-frames intermedios para no acumular latencia
                continue
            elif cli.send_binary_nowait(pkt, track_inflight=False):
                with self._stats_lock:
                    self._bytes_window += len(pkt)
        for cli in dead:
            self.clients.discard(cli)

    def _on_new_dirty_tiles(self, dirty_set: set, w: int, h: int):
        dead = []
        for cli in list(self.clients):
            if cli.closed:
                dead.append(cli)
                continue
            if not cli.authed:
                continue
            cli.pending_tiles.update(dirty_set)
            self._flush_client_tiles(cli)
        for cli in dead:
            self.clients.discard(cli)

    def _flush_client_tiles(self, cli: ClientSession, force_quality=None):
        if cli.closed or not cli.authed or cli.encoding:
            return
        now = time.monotonic()
        if cli.in_flight >= MAX_IN_FLIGHT:
            if (now - cli.last_send_ts) < 0.35:
                return
            cli.in_flight = 0
        if not cli.needs_keyframe and not cli.pending_tiles:
            return

        with self._frame_lock:
            rgb = self._cur_rgb
            w, h = self._cur_w, self._cur_h
        if not rgb:
            return

        cols = (w + TILE_SZ - 1) // TILE_SZ
        rows = (h + TILE_SZ - 1) // TILE_SZ
        total_tiles = max(1, cols * rows)

        full_frame = cli.needs_keyframe or len(cli.pending_tiles) >= total_tiles * 0.45
        if full_frame:
            q = force_quality or (max(38, self.quality - 8) if not cli.needs_keyframe else self.quality)
            tiles_to_send = ()
            was_kf = cli.needs_keyframe
            cli.needs_keyframe = False
            cli.pending_tiles.clear()
        else:
            q = force_quality or self.quality
            tiles_to_send = sorted(cli.pending_tiles)
            was_kf = False
            cli.pending_tiles.clear()

        cli.encoding = True
        asyncio.create_task(
            self._encode_and_send_tiles(cli, rgb, w, h, full_frame, tiles_to_send, q, was_kf)
        )

    async def _encode_and_send_tiles(self, cli: ClientSession, rgb: bytes, w: int, h: int,
                                     full_frame: bool, tiles_to_send, q: int, was_kf: bool):
        try:
            pkt = await self.aio_loop.run_in_executor(
                self._enc_pool, _encode_tiles_packet, rgb, w, h, full_frame, tiles_to_send, q
            )
            if not cli.closed and cli.authed and self.running:
                if cli.send_binary_nowait(pkt, track_inflight=True):
                    with self._stats_lock:
                        self._bytes_window += len(pkt)
                elif was_kf:
                    cli.needs_keyframe = True
        except Exception:
            if was_kf:
                cli.needs_keyframe = True
        finally:
            cli.encoding = False
            if not cli.closed and cli.authed and (cli.needs_keyframe or cli.pending_tiles):
                self._flush_client_tiles(cli)

    def reconfigure(self, width=None, height=None, fps=None, quality=None,
                    position=None, mirror=None, pin=None, codec=None,
                    bitrate=None, use_https=None, keep_fps=None):
        need_restart = False
        if width is not None and int(width) != self.width:
            self.width = max(640, min(2560, int(width)))
            need_restart = True
        if height is not None and int(height) != self.height:
            self.height = max(400, min(1600, int(height)))
            need_restart = True
        if mirror is not None and bool(mirror) != self.mirror:
            self.mirror = bool(mirror)
            need_restart = True
        if codec is not None and codec in ('tiles', 'h264') and codec != self.codec:
            self.codec = codec
            need_restart = True
        if use_https is not None:
            self.use_https = bool(use_https)
        if keep_fps is not None:
            self.keep_fps = bool(keep_fps)
        if bitrate is not None:
            self.bitrate = max(500, min(15000, int(bitrate)))
            if not need_restart and self.x264enc and self.glib_loop:
                br = self.bitrate
                GLib.idle_add(lambda: (self.x264enc.set_property('bitrate', br) if self.x264enc else None) and False)
        if fps is not None:
            self.fps = max(5, min(60, int(fps)))
        if quality is not None:
            self.quality = max(25, min(95, int(quality)))
        if pin is not None:
            self.pin = str(pin).strip()
        if position is not None and position != self.position:
            self.position = position
            if not need_restart and self.glib_loop and self.monitor:
                GLib.idle_add(lambda: self.monitor.arrange_virtual_monitor(self.position) and False)
        if need_restart and self.running and self.glib_loop:
            GLib.idle_add(self._start_display)

    # --- Servidor HTTP + WebSocket (asyncio + TCP_NODELAY) ---

    def _run_asyncio_thread(self):
        self.aio_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.aio_loop)
        try:
            self.aio_loop.run_until_complete(self._serve())
        except Exception as exc:
            self._notify_status(f'Error servidor red: {exc}')
        finally:
            self.aio_loop.close()

    async def _serve(self):
        ssl_ctx = None
        if self.use_https:
            cert, key = ensure_self_signed_cert()
            ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_ctx.load_cert_chain(cert, key)
        self.http_server = await asyncio.start_server(self._handle_conn, self.host, self.port, ssl=ssl_ctx)
        asyncio.create_task(self._sharp_idle_loop())
        async with self.http_server:
            await self.http_server.serve_forever()

    async def _sharp_idle_loop(self):
        """Mantiene actualizados FPS/kbps cada segundo y envía frame nítido tras reposo."""
        while self.running:
            await asyncio.sleep(0.2)
            self._roll_stats()
            if (
                self.codec == 'tiles'
                and self._sharp_pending
                and self.clients
                and (time.monotonic() - self._last_motion_ts) > 0.35
            ):
                self._sharp_pending = False
                sharp_q = min(90, self.quality + 18)
                if sharp_q > self.quality + 2:
                    for cli in list(self.clients):
                        if not cli.closed and cli.authed:
                            cli.needs_keyframe = True
                            self._flush_client_tiles(cli, force_quality=sharp_q)

    async def _handle_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        sock = writer.get_extra_info('socket')
        if sock is not None:
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except Exception:
                pass
        peer = writer.get_extra_info('peername')
        try:
            raw = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), timeout=8)
        except Exception:
            writer.close()
            return

        lines = raw.decode('iso-8859-1', errors='replace').split('\r\n')
        req_line = lines[0].split()
        if len(req_line) < 2:
            writer.close()
            return
        method, path = req_line[0], req_line[1].split('?')[0]
        headers = {}
        for ln in lines[1:]:
            if ':' in ln:
                k, v = ln.split(':', 1)
                headers[k.strip().lower()] = v.strip()

        if headers.get('upgrade', '').lower() == 'websocket' and 'sec-websocket-key' in headers:
            await self._handle_ws(reader, writer, peer, headers['sec-websocket-key'])
            return

        if method not in ('GET', 'HEAD'):
            writer.write(b'HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\n\r\n')
            await writer.drain()
            writer.close()
            return

        if path == '/deskcast.apk':
            apk_path = os.path.join(BASE_DIR, 'android', 'DeskCast.apk')
            if not os.path.isfile(apk_path):
                writer.write(b'HTTP/1.1 404 Not Found\r\nConnection: close\r\n\r\nAPK no compilado')
                await writer.drain()
                writer.close()
                return
            with open(apk_path, 'rb') as f:
                data = f.read()
            writer.write(
                b'HTTP/1.1 200 OK\r\nContent-Type: application/vnd.android.package-archive\r\n'
                b'Content-Disposition: attachment; filename="DeskCast.apk"\r\n'
                + f'Content-Length: {len(data)}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n'.encode('ascii')
                + (b'' if method == 'HEAD' else data)
            )
            await writer.drain()
            writer.close()
            return

        if path == '/api/status':
            peers = [c.peer[0] for c in list(self.clients) if not c.closed and c.peer]
            body = json.dumps({
                'app': 'DeskCast',
                'running': self.running,
                'width': self.width,
                'height': self.height,
                'fps': self.fps,
                'actual_fps': self.actual_fps,
                'kbps': self.kbps,
                'quality': self.quality,
                'bitrate': self.bitrate,
                'codec': self.codec,
                'https': self.use_https,
                'position': self.position,
                'mirror': self.mirror,
                'clients': len(self.clients),
                'peers': peers,
                'status': self.status_text,
            }).encode('utf-8')
            writer.write(
                b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n'
                + f'Content-Length: {len(body)}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n'.encode('ascii')
                + (b'' if method == 'HEAD' else body)
            )
            await writer.drain()
            writer.close()
            return

        rel = 'index.html' if path in ('/', '') else path.lstrip('/')
        safe_path = os.path.realpath(os.path.join(WEB_DIR, rel))
        if not safe_path.startswith(os.path.realpath(WEB_DIR)) or not os.path.isfile(safe_path):
            writer.write(b'HTTP/1.1 404 Not Found\r\nConnection: close\r\n\r\n404')
            await writer.drain()
            writer.close()
            return

        ctype, _ = mimetypes.guess_type(safe_path)
        ctype = ctype or 'application/octet-stream'
        with open(safe_path, 'rb') as f:
            data = f.read()
        writer.write(
            f'HTTP/1.1 200 OK\r\nContent-Type: {ctype}; charset=utf-8\r\n'
            f'Content-Length: {len(data)}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n'.encode('ascii')
            + (b'' if method == 'HEAD' else data)
        )
        await writer.drain()
        writer.close()

    async def _handle_ws(self, reader, writer, peer, ws_key):
        accept = base64.b64encode(hashlib.sha1((ws_key + WS_GUID).encode('ascii')).digest()).decode('ascii')
        resp = (
            'HTTP/1.1 101 Switching Protocols\r\n'
            'Upgrade: websocket\r\n'
            'Connection: Upgrade\r\n'
            f'Sec-WebSocket-Accept: {accept}\r\n\r\n'
        )
        writer.write(resp.encode('ascii'))
        await writer.drain()

        need_pin = bool(self.pin)
        cli = ClientSession(reader, writer, peer, need_pin)
        self.clients.add(cli)

        hello = json.dumps({
            'type': 'hello',
            'width': self.width,
            'height': self.height,
            'fps': self.fps,
            'quality': self.quality,
            'codec': self.codec,
            'position': self.position,
            'mirror': self.mirror,
            'needPin': need_pin,
            'authed': cli.authed,
        }).encode('utf-8')
        await cli.send_ws_frame(0x1, hello)
        if cli.authed:
            if self.codec == 'tiles':
                self._flush_client_tiles(cli)
            elif self.codec == 'h264':
                if self._last_h264_kf:
                    cli.send_binary_nowait(self._last_h264_kf, track_inflight=False)
                if self.glib_loop:
                    GLib.idle_add(self._force_h264_keyframe)

        try:
            while not cli.closed:
                opcode, payload = await self._read_ws_frame(reader)
                if opcode is None or opcode == 0x8:
                    break
                if opcode == 0x9:
                    await cli.send_ws_frame(0xA, payload)
                    continue
                if opcode == 0x1:
                    await self._handle_ws_msg(cli, payload.decode('utf-8', errors='replace'))
        except Exception:
            pass
        finally:
            cli.closed = True
            self.clients.discard(cli)
            try:
                writer.close()
            except Exception:
                pass

    async def _read_ws_frame(self, reader: asyncio.StreamReader):
        hdr = await reader.readexactly(2)
        b1, b2 = hdr[0], hdr[1]
        opcode = b1 & 0x0F
        masked = bool(b2 & 0x80)
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack('!H', await reader.readexactly(2))[0]
        elif length == 127:
            length = struct.unpack('!Q', await reader.readexactly(8))[0]
        if length > 4 * 1024 * 1024:
            return None, b''
        mask = await reader.readexactly(4) if masked else None
        data = bytearray(await reader.readexactly(length))
        if mask:
            for i in range(length):
                data[i] ^= mask[i % 4]
        return opcode, bytes(data)

    async def _handle_ws_msg(self, cli: ClientSession, raw_txt: str):
        try:
            msg = json.loads(raw_txt)
        except Exception:
            return
        mtype = msg.get('type')

        if mtype == 'ping':
            await cli.send_ws_frame(0x1, json.dumps({'type': 'pong', 't': msg.get('t', 0)}).encode('utf-8'))
            return

        if mtype == 'ack':
            cli.in_flight = max(0, cli.in_flight - 1)
            if self.codec == 'tiles':
                self._flush_client_tiles(cli)
            return

        if mtype == 'caps':
            cli.has_webcodecs = bool(msg.get('webcodecs', True))
            if self.codec == 'h264' and not cli.has_webcodecs:
                # Si el navegador está en HTTP remoto sin WebCodecs, volver a Turbo Tiles
                self.reconfigure(codec='tiles')
                upd = json.dumps({
                    'type': 'hello',
                    'width': self.width,
                    'height': self.height,
                    'fps': self.fps,
                    'quality': self.quality,
                    'codec': self.codec,
                    'position': self.position,
                    'mirror': self.mirror,
                    'needPin': bool(self.pin),
                    'authed': cli.authed,
                }).encode('utf-8')
                await cli.send_ws_frame(0x1, upd)
            return

        if mtype == 'keyframe':
            if self.codec == 'h264':
                if self._last_h264_kf:
                    cli.send_binary_nowait(self._last_h264_kf, track_inflight=False)
                if self.glib_loop:
                    GLib.idle_add(self._force_h264_keyframe)
            else:
                cli.needs_keyframe = True
                self._flush_client_tiles(cli)
            return

        if mtype == 'auth':
            if not self.pin or str(msg.get('pin', '')).strip() == self.pin:
                cli.authed = True
                cli.needs_keyframe = True
                await cli.send_ws_frame(0x1, json.dumps({'type': 'auth_ok'}).encode('utf-8'))
                if self.codec == 'tiles':
                    self._flush_client_tiles(cli)
                elif self.codec == 'h264':
                    if self._last_h264_kf:
                        cli.send_binary_nowait(self._last_h264_kf, track_inflight=False)
                    if self.glib_loop:
                        GLib.idle_add(self._force_h264_keyframe)
            else:
                await cli.send_ws_frame(0x1, json.dumps({'type': 'auth_err'}).encode('utf-8'))
            return

        if not cli.authed:
            return

        if mtype == 'pointer' and self.monitor:
            act = msg.get('action')
            x, y = msg.get('x', 0.5), msg.get('y', 0.5)
            if act == 'move':
                GLib.idle_add(lambda: self.monitor.move_pointer_abs(x, y) and False)
            elif act == 'down':
                btn = msg.get('button', 0)
                GLib.idle_add(lambda: (self.monitor.move_pointer_abs(x, y), self.monitor.pointer_button(btn, True)) and False)
            elif act == 'up':
                btn = msg.get('button', 0)
                GLib.idle_add(lambda: (self.monitor.move_pointer_abs(x, y), self.monitor.pointer_button(btn, False)) and False)
            elif act == 'scroll':
                dx, dy = msg.get('dx', 0), msg.get('dy', 0)
                GLib.idle_add(lambda: self.monitor.pointer_scroll(dx, dy) and False)
        elif mtype == 'touch' and self.monitor:
            act = msg.get('action')
            slot = msg.get('id', 0)
            x, y = msg.get('x', 0.5), msg.get('y', 0.5)
            if act == 'down':
                GLib.idle_add(lambda: self.monitor.touch_down(slot, x, y) and False)
            elif act == 'move':
                GLib.idle_add(lambda: self.monitor.touch_motion(slot, x, y) and False)
            elif act == 'up':
                GLib.idle_add(lambda: self.monitor.touch_up(slot) and False)
        elif mtype == 'key' and self.monitor:
            ks = int(msg.get('keysym', 0))
            pr = bool(msg.get('pressed', True))
            if ks:
                GLib.idle_add(lambda: self.monitor.key_sym(ks, pr) and False)
        elif mtype == 'config':
            self.reconfigure(
                width=msg.get('width'),
                height=msg.get('height'),
                fps=msg.get('fps'),
                quality=msg.get('quality'),
                position=msg.get('position'),
                mirror=msg.get('mirror'),
                codec=msg.get('codec'),
                bitrate=msg.get('bitrate'),
            )
            upd = json.dumps({
                'type': 'hello',
                'width': self.width,
                'height': self.height,
                'fps': self.fps,
                'quality': self.quality,
                'codec': self.codec,
                'position': self.position,
                'mirror': self.mirror,
                'needPin': bool(self.pin),
                'authed': cli.authed,
            }).encode('utf-8')
            for c in list(self.clients):
                asyncio.create_task(c.send_ws_frame(0x1, upd))


def main():
    ap = argparse.ArgumentParser(description='DeskCast: Segundo monitor virtual para tablet (GNOME Wayland)')
    ap.add_argument('--host', default='0.0.0.0')
    ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--width', type=int, default=1280)
    ap.add_argument('--height', type=int, default=800)
    ap.add_argument('--fps', type=int, default=30)
    ap.add_argument('--quality', type=int, default=58)
    ap.add_argument('--bitrate', type=int, default=4000)
    ap.add_argument('--position', choices=['right', 'left', 'top', 'bottom'], default='right')
    ap.add_argument('--mirror', action='store_true')
    ap.add_argument('--codec', choices=['tiles', 'h264'], default='h264')
    ap.add_argument('--https', action='store_true')
    ap.add_argument('--pin', default='')
    args = ap.parse_args()

    srv = DeskCastServer(
        host=args.host, port=args.port, width=args.width, height=args.height,
        fps=args.fps, quality=args.quality, position=args.position,
        mirror=args.mirror, pin=args.pin, codec=args.codec, use_https=args.https,
        bitrate=args.bitrate,
    )
    srv.on_status_change = lambda m: print(f'[deskcast] {m}', flush=True)
    srv.start_background()

    proto = 'https' if args.https else 'http'
    nets = detectar_redes()
    print('\n=== DESKCAST ACTIVO ===')
    for iface, ip in nets['tailscale']:
        print(f'  [Tailscale · Fuera de red] {proto}://{ip}:{args.port}')
    for iface, ip in nets['wireguard']:
        print(f'  [WireGuard · Fuera de red] {proto}://{ip}:{args.port}')
    for iface, ip in nets['lan']:
        print(f'  [Red Local ({iface})]      {proto}://{ip}:{args.port}')
    print('Pulsa Ctrl+C para detener.\n', flush=True)

    stop_ev = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop_ev.set())
    signal.signal(signal.SIGTERM, lambda *_: stop_ev.set())
    while not stop_ev.is_set():
        stop_ev.wait(0.5)
    srv.stop()


if __name__ == '__main__':
    main()
