"""Gestión de monitor virtual (Mutter ScreenCast + DisplayConfig + RemoteDesktop) en GNOME Wayland."""
import time

import gi

gi.require_version('Gio', '2.0')
from gi.repository import Gio, GLib  # noqa: E402

SC_BUS = 'org.gnome.Mutter.ScreenCast'
SC_PATH = '/org/gnome/Mutter/ScreenCast'
RD_BUS = 'org.gnome.Mutter.RemoteDesktop'
RD_PATH = '/org/gnome/Mutter/RemoteDesktop'
DC_BUS = 'org.gnome.Mutter.DisplayConfig'
DC_PATH = '/org/gnome/Mutter/DisplayConfig'

# Códigos evdev estándar para ratón
BTN_LEFT = 0x110
BTN_RIGHT = 0x111
BTN_MIDDLE = 0x112


class MutterVirtualMonitor:
    """Crea un monitor virtual extendido (o espejo) en GNOME Wayland con entrada táctil/ratón."""

    def __init__(self, width=1280, height=800, position='right', mirror=False):
        self.width = int(width)
        self.height = int(height)
        self.position = position  # 'right', 'left', 'top', 'bottom'
        self.mirror = bool(mirror)

        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.rd_path = None
        self.sc_path = None
        self.stream_path = None
        self.node_id = None
        self._sub_id = None
        self._closed_sub_id = None
        self.virt_origin = None
        self.running = False

    def _has_active_virtual_monitor(self):
        """Comprueba si Mutter ya tiene algún monitor virtual Meta-* activo."""
        try:
            st = self.bus.call_sync(
                DC_BUS, DC_PATH, DC_BUS, 'GetCurrentState',
                None, None, Gio.DBusCallFlags.NONE, -1, None,
            ).unpack()
            for m in st[1]:
                if m[0][0].startswith('Meta-'):
                    return True
        except Exception:
            pass
        return False

    def start(self, on_node_ready, on_closed=None, timeout_ms=5000, wait_clear=False):
        """Crea sesión RemoteDesktop + ScreenCast y llama a on_node_ready(node_id)."""
        had_session = self.rd_path is not None or wait_clear
        self.stop()

        if not self.mirror:
            if had_session:
                # Dar margen a Mutter para retirar el Meta-* previo al reconfigurar
                for _ in range(15):
                    if not self._has_active_virtual_monitor():
                        break
                    time.sleep(0.08)
            if self._has_active_virtual_monitor():
                raise RuntimeError(
                    'Ya existe otro monitor virtual Meta-* activo; cierra la otra instancia antes.'
                )

        rd_res = self.bus.call_sync(
            RD_BUS, RD_PATH, RD_BUS, 'CreateSession',
            None, None, Gio.DBusCallFlags.NONE, -1, None,
        )
        self.rd_path = rd_res.unpack()[0]

        rd_sid = self.bus.call_sync(
            RD_BUS, self.rd_path, 'org.freedesktop.DBus.Properties', 'Get',
            GLib.Variant('(ss)', ('org.gnome.Mutter.RemoteDesktop.Session', 'SessionId')),
            None, Gio.DBusCallFlags.NONE, -1, None,
        ).unpack()[0]

        sc_res = self.bus.call_sync(
            SC_BUS, SC_PATH, SC_BUS, 'CreateSession',
            GLib.Variant('(a{sv})', ({'remote-desktop-session-id': GLib.Variant('s', rd_sid)},)),
            None, Gio.DBusCallFlags.NONE, -1, None,
        )
        self.sc_path = sc_res.unpack()[0]

        if self.mirror:
            connector = self._primary_connector()
            self.stream_path = self.bus.call_sync(
                SC_BUS, self.sc_path, 'org.gnome.Mutter.ScreenCast.Session', 'RecordMonitor',
                GLib.Variant('(sa{sv})', (connector, {'cursor-mode': GLib.Variant('u', 1)})),
                None, Gio.DBusCallFlags.NONE, -1, None,
            ).unpack()[0]
        else:
            self.stream_path = self.bus.call_sync(
                SC_BUS, self.sc_path, 'org.gnome.Mutter.ScreenCast.Session', 'RecordVirtual',
                GLib.Variant('(a{sv})', ({
                    'cursor-mode': GLib.Variant('u', 1),
                    'is-platform': GLib.Variant('b', True),
                },)),
                None, Gio.DBusCallFlags.NONE, -1, None,
            ).unpack()[0]

        def _on_stream_added(_conn, _sender, _path, _iface, _sig, params):
            self.node_id = int(params.unpack()[0])
            self.running = True
            on_node_ready(self.node_id)

        self._sub_id = self.bus.signal_subscribe(
            SC_BUS, 'org.gnome.Mutter.ScreenCast.Stream', 'PipeWireStreamAdded',
            self.stream_path, None, Gio.DBusSignalFlags.NONE, _on_stream_added,
        )

        if on_closed:
            def _on_closed(_conn, _sender, _path, _iface, _sig, _params):
                self.running = False
                on_closed()
            self._closed_sub_id = self.bus.signal_subscribe(
                RD_BUS, 'org.gnome.Mutter.RemoteDesktop.Session', 'Closed',
                self.rd_path, None, Gio.DBusSignalFlags.NONE, _on_closed,
            )

        self.bus.call_sync(
            RD_BUS, self.rd_path, 'org.gnome.Mutter.RemoteDesktop.Session', 'Start',
            None, None, Gio.DBusCallFlags.NONE, -1, None,
        )

    def stop(self):
        self.running = False
        if self._sub_id is not None:
            try:
                self.bus.signal_unsubscribe(self._sub_id)
            except Exception:
                pass
            self._sub_id = None
        if self._closed_sub_id is not None:
            try:
                self.bus.signal_unsubscribe(self._closed_sub_id)
            except Exception:
                pass
            self._closed_sub_id = None
        if self.rd_path:
            try:
                self.bus.call_sync(
                    RD_BUS, self.rd_path, 'org.gnome.Mutter.RemoteDesktop.Session', 'Stop',
                    None, None, Gio.DBusCallFlags.NONE, -1, None,
                )
            except Exception:
                pass
        self.rd_path = None
        self.sc_path = None
        self.stream_path = None
        self.node_id = None

    def _primary_connector(self):
        st = self.bus.call_sync(
            DC_BUS, DC_PATH, DC_BUS, 'GetCurrentState',
            None, None, Gio.DBusCallFlags.NONE, -1, None,
        ).unpack()
        for lm in st[2]:
            if lm[4]:  # is_primary
                return lm[5][0][0]
        return st[1][0][0][0] if st[1] else 'DP-1'

    def arrange_virtual_monitor(self, position=None):
        """Coloca el monitor virtual Meta-0 respecto al monitor principal (right/left/top/bottom)."""
        if self.mirror:
            return False
        pos = position or self.position
        self.position = pos
        try:
            st = self.bus.call_sync(
                DC_BUS, DC_PATH, DC_BUS, 'GetCurrentState',
                None, None, Gio.DBusCallFlags.NONE, -1, None,
            ).unpack()
        except Exception:
            return False

        serial = st[0]
        monitors = st[1]
        logicals = st[2]
        if len(monitors) < 2:
            return False

        # Identificar monitor principal y virtual (Meta-*)
        mon_modes = {}
        for m in monitors:
            conn = m[0][0]
            cur_mode = None
            for mode in m[1]:
                if mode[6].get('is-current'):
                    cur_mode = (mode[0], int(mode[1]), int(mode[2]))
                    break
            if not cur_mode and m[1]:
                mode = m[1][0]
                cur_mode = (mode[0], int(mode[1]), int(mode[2]))
            mon_modes[conn] = cur_mode

        prim_lm = None
        virt_conn = None
        for lm in logicals:
            conns = [c[0] for c in lm[5]]
            if lm[4] or (prim_lm is None and not any(c.startswith('Meta-') for c in conns)):
                prim_lm = lm
            for c in conns:
                if c.startswith('Meta-'):
                    virt_conn = c
        if not virt_conn:
            for c in mon_modes:
                if c.startswith('Meta-'):
                    virt_conn = c
                    break
        if not prim_lm or not virt_conn or virt_conn not in mon_modes:
            return False

        prim_conn = prim_lm[5][0][0]
        prim_mode = mon_modes[prim_conn]
        virt_mode = mon_modes[virt_conn]
        if not prim_mode or not virt_mode:
            return False

        pw, ph = prim_mode[1], prim_mode[2]
        vw, vh = virt_mode[1], virt_mode[2]
        pscale = float(prim_lm[2]) or 1.0
        ptrans = int(prim_lm[3])

        # Calcular coordenadas lógicas evitando negativos cuando es left/top
        if pos == 'left':
            px, py = vw, 0
            vx, vy = 0, max(0, (ph - vh) // 2)
        elif pos == 'top':
            px, py = 0, vh
            vx, vy = max(0, (pw - vw) // 2), 0
        elif pos == 'bottom':
            px, py = 0, 0
            vx, vy = max(0, (pw - vw) // 2), ph
        else:  # 'right'
            px, py = 0, 0
            vx, vy = pw, max(0, (ph - vh) // 2)

        cfg = [
            (px, py, pscale, ptrans, True, [(prim_conn, prim_mode[0], {})]),
            (vx, vy, 1.0, 0, False, [(virt_conn, virt_mode[0], {})]),
        ]
        try:
            self.bus.call_sync(
                DC_BUS, DC_PATH, DC_BUS, 'ApplyMonitorsConfig',
                GLib.Variant('(uua(iiduba(ssa{sv}))a{sv})', (serial, 1, cfg, {})),
                None, Gio.DBusCallFlags.NONE, -1, None,
            )
            self.virt_origin = (int(vx), int(vy))
            return True
        except Exception:
            return False

    # --- Inyección de entrada (Ratón, Táctil y Teclado) ---

    def _call_rd(self, method, sig, *args):
        if not self.rd_path or not self.running:
            return
        try:
            self.bus.call(
                RD_BUS, self.rd_path, 'org.gnome.Mutter.RemoteDesktop.Session',
                method, GLib.Variant(sig, args), None,
                Gio.DBusCallFlags.NONE, -1, None, None, None,
            )
        except Exception:
            pass

    def move_pointer_abs(self, norm_x, norm_y):
        if not self.stream_path:
            return
        x = max(0.0, min(1.0, float(norm_x))) * self.width
        y = max(0.0, min(1.0, float(norm_y))) * self.height
        self._call_rd('NotifyPointerMotionAbsolute', '(sdd)', self.stream_path, x, y)

    def pointer_button(self, button_idx, pressed):
        code = {0: BTN_LEFT, 1: BTN_MIDDLE, 2: BTN_RIGHT}.get(int(button_idx), BTN_LEFT)
        self._call_rd('NotifyPointerButton', '(ib)', code, bool(pressed))

    def pointer_scroll(self, dx, dy):
        self._call_rd('NotifyPointerAxis', '(ddu)', float(dx), float(dy), 0)

    def touch_down(self, slot, norm_x, norm_y):
        if not self.stream_path:
            return
        x = max(0.0, min(1.0, float(norm_x))) * self.width
        y = max(0.0, min(1.0, float(norm_y))) * self.height
        self._call_rd('NotifyTouchDown', '(sudd)', self.stream_path, int(slot), x, y)

    def touch_motion(self, slot, norm_x, norm_y):
        if not self.stream_path:
            return
        x = max(0.0, min(1.0, float(norm_x))) * self.width
        y = max(0.0, min(1.0, float(norm_y))) * self.height
        self._call_rd('NotifyTouchMotion', '(sudd)', self.stream_path, int(slot), x, y)

    def touch_up(self, slot):
        self._call_rd('NotifyTouchUp', '(u)', int(slot))

    def key_sym(self, keysym, pressed):
        self._call_rd('NotifyKeyboardKeysym', '(ub)', int(keysym), bool(pressed))
