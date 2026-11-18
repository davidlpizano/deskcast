#!/usr/bin/env python3
"""DeskCast — App gráfica GTK4 / Libadwaita para usar tu tablet como 2º monitor en GNOME Wayland."""
import os
import signal
import subprocess
import sys
import threading

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from deskcast_server import DeskCastServer  # noqa: E402
from qr_lite import qr_to_png_bytes  # noqa: E402
from red_remota import (  # noqa: E402
    activar_adb_reverse,
    activar_hotspot,
    comando_instalar_tailscale,
    desactivar_hotspot,
    detectar_redes,
    estado_adb,
    estado_hotspot,
    estado_tailscale,
    estado_wireguard,
    find_adb,
)
from updater import apply_update, check_for_updates, get_local_version  # noqa: E402


class DeskCastWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        ver = get_local_version()
        super().__init__(application=app, title=f'DeskCast v{ver} · 2º Monitor Wayland')
        self.set_default_size(900, 680)
        self.set_icon_name('deskcast')
        self.server = DeskCastServer(codec='h264', bitrate=4000)
        self._qr_url_actual = ''
        self._syncing_preset = False

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(root)

        header = Adw.HeaderBar()
        self.btn_update = Gtk.Button(label=f'↻ Buscar actualizaciones (v{ver})')
        self.btn_update.set_tooltip_text('Comprobar nuevas versiones en GitHub')
        self.btn_update.connect('clicked', self._on_check_updates)
        header.pack_start(self.btn_update)

        self.btn_toggle = Gtk.Button(label='▶ Iniciar 2º Monitor')
        self.btn_toggle.add_css_class('suggested-action')
        self.btn_toggle.connect('clicked', self._on_toggle)
        header.pack_end(self.btn_toggle)
        root.append(header)

        self.banner = Adw.Banner(title='Pulsa "Iniciar 2º Monitor" y conecta desde la app DeskCast o navegador')
        self.banner.set_revealed(True)
        root.append(self.banner)

        scroll = Gtk.ScrolledWindow(vexpand=True)
        root.append(scroll)

        main_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=18,
            margin_top=18, margin_bottom=18, margin_start=18, margin_end=18,
        )
        scroll.set_child(main_box)

        # Columna izquierda: Configuración
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, hexpand=True)
        main_box.append(left)

        grp_mon = Adw.PreferencesGroup(title='Monitor Virtual (GNOME Wayland)')
        left.append(grp_mon)

        self.row_preset = Adw.ComboRow(title='Perfil de rendimiento')
        self.row_preset.set_model(Gtk.StringList.new([
            'Equilibrado (1280×800 · H.264 4000 kbps)',
            '⚡ Ultra Fluido (1280×800 · 60 FPS · H.264 6000 kbps)',
            '🎨 Nítido / Lectura (1920×1200 · 30 FPS · H.264 8000 kbps)',
            '📱 Ahorro 4G / Remoto (1024×640 · 25 FPS · Turbo Tiles)',
        ]))
        self.row_preset.set_selected(0)
        self.row_preset.connect('notify::selected', self._on_preset_change)
        grp_mon.add(self.row_preset)

        self.row_res = Adw.ComboRow(title='Resolución de la tablet')
        self.res_vals = [(1024, 640), (1280, 800), (1600, 1000), (1920, 1200), (1920, 1080)]
        self.row_res.set_model(Gtk.StringList.new([
            '1024×640 (Turbo 4G)', '1280×800 (Equilibrado)', '1600×1000 (Nítido)', '1920×1200 (HD+)', '1920×1080 (16:9)'
        ]))
        self.row_res.set_selected(1)
        self.row_res.connect('notify::selected', self._on_cfg_change)
        grp_mon.add(self.row_res)

        self.row_pos = Adw.ComboRow(title='Posición respecto al monitor principal')
        self.pos_vals = ['right', 'left', 'top', 'bottom']
        self.row_pos.set_model(Gtk.StringList.new(['Derecha', 'Izquierda', 'Arriba', 'Abajo']))
        self.row_pos.set_selected(0)
        self.row_pos.connect('notify::selected', self._on_cfg_change)
        grp_mon.add(self.row_pos)

        self.row_mirror = Adw.SwitchRow(title='Modo espejo (clonar pantalla principal)')
        self.row_mirror.connect('notify::active', self._on_cfg_change)
        grp_mon.add(self.row_mirror)

        grp_str = Adw.PreferencesGroup(title='Fluidez, Códec y Seguridad')
        left.append(grp_str)

        self.row_fps = Adw.SpinRow.new_with_range(10, 60, 1)
        self.row_fps.set_title('FPS objetivo (24 / 30 / 60)')
        self.row_fps.set_value(30)
        self.row_fps.connect('notify::value', self._on_cfg_change)
        grp_str.add(self.row_fps)

        self.row_keep_fps = Adw.SwitchRow(
            title='Mantener FPS estables en reposo (H.264)',
            subtitle='Emite micro-frames de ~60 bytes cuando no hay movimiento (~14 kbps)',
        )
        self.row_keep_fps.set_active(True)
        self.row_keep_fps.connect('notify::active', self._on_cfg_change)
        grp_str.add(self.row_keep_fps)

        self.row_qual = Adw.SpinRow.new_with_range(25, 95, 5)
        self.row_qual.set_title('Calidad Turbo Tiles (%)')
        self.row_qual.set_value(58)
        self.row_qual.connect('notify::value', self._on_cfg_change)
        grp_str.add(self.row_qual)

        self.row_codec = Adw.ComboRow(title='Códec de streaming')
        self.codec_vals = ['tiles', 'h264']
        self.row_codec.set_model(Gtk.StringList.new([
            'Turbo Tiles (JPEG · Compatible HTTP)',
            'H.264 (60 FPS · App Android / HTTPS / USB)',
        ]))
        self.row_codec.set_selected(1)
        self.row_codec.connect('notify::selected', self._on_cfg_change)
        grp_str.add(self.row_codec)

        self.row_bitrate = Adw.SpinRow.new_with_range(1000, 12000, 500)
        self.row_bitrate.set_title('Bitrate H.264 (kbps)')
        self.row_bitrate.set_value(4000)
        self.row_bitrate.connect('notify::value', self._on_cfg_change)
        grp_str.add(self.row_bitrate)

        self.row_https = Adw.SwitchRow(title='HTTPS (para H.264 en navegador por Wi-Fi)')
        self.row_https.connect('notify::active', self._on_https_toggle)
        grp_str.add(self.row_https)

        self.row_pin = Adw.EntryRow(title='PIN opcional (vacío = sin PIN)')
        self.row_pin.connect('changed', self._on_cfg_change)
        grp_str.add(self.row_pin)

        # Conexión directa (Hotspot Wi-Fi + Cable USB ADB + App Android)
        grp_usb = Adw.PreferencesGroup(
            title='Conexión Directa Alta Velocidad (Hotspot / USB / App Android)',
            description='Usa la app nativa DeskCast (.apk) por Hotspot 5 GHz o USB para obtener H.264 por hardware.',
        )
        left.append(grp_usb)

        self.row_hotspot = Adw.ActionRow(title='Hotspot Wi-Fi directo (antena del PC)')
        self.btn_hotspot = Gtk.Button(label='Encender Hotspot', valign=Gtk.Align.CENTER)
        self.btn_hotspot.connect('clicked', self._on_toggle_hotspot)
        self.row_hotspot.add_suffix(self.btn_hotspot)
        grp_usb.add(self.row_hotspot)

        self.row_usb = Adw.ActionRow(title='Cable USB directo (ADB reverse)')
        btn_usb = Gtk.Button(label='Conectar por USB', valign=Gtk.Align.CENTER)
        btn_usb.add_css_class('suggested-action')
        btn_usb.connect('clicked', self._on_connect_usb)
        self.row_usb.add_suffix(btn_usb)
        grp_usb.add(self.row_usb)

        self.row_apk = Adw.ActionRow(
            title='App Android nativa DeskCast (.apk)',
            subtitle='Decodificación H.264 por hardware + Wi-Fi baja latencia + teclado',
        )
        btn_apk_qr = Gtk.Button(label='QR Descarga APK', valign=Gtk.Align.CENTER)
        btn_apk_qr.connect('clicked', self._on_apk_qr)
        self.row_apk.add_suffix(btn_apk_qr)
        btn_apk_usb = Gtk.Button(label='Instalar por USB', valign=Gtk.Align.CENTER)
        btn_apk_usb.connect('clicked', self._on_apk_install_usb)
        self.row_apk.add_suffix(btn_apk_usb)
        grp_usb.add(self.row_apk)

        # Acceso remoto (Tailscale / WireGuard)
        grp_vpn = Adw.PreferencesGroup(
            title='Acceso fuera de la misma red (Tailscale / WireGuard)',
            description='Conecta por 4G/5G u otro Wi-Fi con túnel VPN cifrado.',
        )
        left.append(grp_vpn)

        self.row_ts = Adw.ActionRow(title='Tailscale VPN')
        btn_ts = Gtk.Button(label='Instalar / Conectar', valign=Gtk.Align.CENTER)
        btn_ts.connect('clicked', self._on_setup_tailscale)
        self.row_ts.add_suffix(btn_ts)
        grp_vpn.add(self.row_ts)

        self.row_wg = Adw.ActionRow(title='WireGuard (/usr/bin/wg)')
        btn_wg = Gtk.Button(label='Guía WireGuard', valign=Gtk.Align.CENTER)
        btn_wg.connect('clicked', self._on_info_wireguard)
        self.row_wg.add_suffix(btn_wg)
        grp_vpn.add(self.row_wg)

        # Columna derecha: QR, URLs y telemetría
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        right.set_size_request(300, -1)
        main_box.append(right)

        lbl_qr = Gtk.Label(label='Escanea desde tu tablet', css_classes=['title-3'])
        right.append(lbl_qr)

        self.qr_pic = Gtk.Picture(can_shrink=True)
        self.qr_pic.set_size_request(230, 230)
        frame_qr = Gtk.Frame()
        frame_qr.set_child(self.qr_pic)
        right.append(frame_qr)

        url_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.lbl_url = Gtk.Label(selectable=True, wrap=True, hexpand=True, css_classes=['monospace'])
        url_box.append(self.lbl_url)
        btn_copy = Gtk.Button(label='Copiar')
        btn_copy.connect('clicked', self._on_copy_url)
        url_box.append(btn_copy)
        right.append(url_box)

        self.combo_net = Gtk.DropDown()
        self.combo_net.connect('notify::selected', self._on_select_url)
        right.append(self.combo_net)

        self.lbl_stats = Gtk.Label(label='Tablets: 0 · 0 FPS · 0 kbps', css_classes=['dim-label'])
        right.append(self.lbl_stats)

        self.lbl_peers = Gtk.Label(label='', css_classes=['caption', 'dim-label'], wrap=True)
        right.append(self.lbl_peers)

        btn_ref = Gtk.Button(label='↻ Actualizar IPs y VPN')
        btn_ref.connect('clicked', lambda *_: self._refresh_nets())
        right.append(btn_ref)

        btn_ping_mon = Gtk.Button(label='🖱 Mover cursor al 2º monitor')
        btn_ping_mon.connect('clicked', self._on_test_cursor)
        right.append(btn_ping_mon)

        self._url_list = []
        self._refresh_nets()
        GLib.timeout_add(1000, self._tick_stats)
        self.connect('close-request', self._on_close)

    def _on_close(self, *_):
        self.server.stop()
        return False

    def _on_preset_change(self, *_):
        idx = self.row_preset.get_selected()
        self._syncing_preset = True
        try:
            if idx == 0:
                self.row_res.set_selected(1)
                self.row_fps.set_value(30)
                self.row_codec.set_selected(1)
                self.row_bitrate.set_value(4000)
            elif idx == 1:
                self.row_res.set_selected(1)
                self.row_fps.set_value(60)
                self.row_codec.set_selected(1)
                self.row_bitrate.set_value(6000)
            elif idx == 2:
                self.row_res.set_selected(3)
                self.row_fps.set_value(30)
                self.row_codec.set_selected(1)
                self.row_bitrate.set_value(8000)
            elif idx == 3:
                self.row_res.set_selected(0)
                self.row_fps.set_value(25)
                self.row_codec.set_selected(0)
                self.row_qual.set_value(50)
        finally:
            self._syncing_preset = False
        self._apply_cfg()

    def _on_toggle(self, *_):
        if self.server.running:
            self.server.stop()
            self.btn_toggle.set_label('▶ Iniciar 2º Monitor')
            self.btn_toggle.remove_css_class('destructive-action')
            self.btn_toggle.add_css_class('suggested-action')
            self.banner.set_title('Monitor virtual detenido')
        else:
            self._apply_cfg()
            self.server.on_status_change = lambda m: GLib.idle_add(self.banner.set_title, m)
            self.server.start_background()
            self.btn_toggle.set_label('■ Detener 2º Monitor')
            self.btn_toggle.remove_css_class('suggested-action')
            self.btn_toggle.add_css_class('destructive-action')
            self._refresh_nets()

    def _apply_cfg(self):
        if self._syncing_preset:
            return
        w, h = self.res_vals[self.row_res.get_selected()]
        pos = self.pos_vals[self.row_pos.get_selected()]
        codec = self.codec_vals[self.row_codec.get_selected()]
        self.server.reconfigure(
            width=w,
            height=h,
            fps=int(self.row_fps.get_value()),
            quality=int(self.row_qual.get_value()),
            position=pos,
            mirror=self.row_mirror.get_active(),
            pin=self.row_pin.get_text(),
            codec=codec,
            bitrate=int(self.row_bitrate.get_value()),
            use_https=self.row_https.get_active(),
            keep_fps=self.row_keep_fps.get_active(),
        )

    def _on_https_toggle(self, *_):
        was_running = self.server.running
        if was_running:
            self.server.stop()
        self._apply_cfg()
        if was_running:
            self.server.on_status_change = lambda m: GLib.idle_add(self.banner.set_title, m)
            self.server.start_background()
        self._refresh_nets()

    def _on_cfg_change(self, *_):
        self._apply_cfg()

    def _refresh_nets(self):
        proto = 'https' if self.row_https.get_active() else 'http'
        hs_active, hs_dev, hs_ip, hs_ssid = estado_hotspot()
        if hs_active:
            self.row_hotspot.set_subtitle(f'Activo ({hs_ssid}) · IP: {hs_ip or "10.42.0.1"} ({hs_dev})')
            self.btn_hotspot.set_label('Apagar Hotspot')
        else:
            self.row_hotspot.set_subtitle(f'Apagado (antena {hs_dev})' if hs_dev else 'Sin antena Wi-Fi')
            self.btn_hotspot.set_label('Encender Hotspot')

        adb_st, adb_detail = estado_adb()
        usb_ready = False
        if adb_st == 'conectado':
            ok_rev, _ = activar_adb_reverse(self.server.port)
            usb_ready = ok_rev
            self.row_usb.set_subtitle(
                f'Activo ({adb_detail}) → {proto}://127.0.0.1:{self.server.port}'
                if ok_rev else f'Tablet detectada ({adb_detail}), pulsa Conectar por USB'
            )
        elif adb_st == 'sin_autorizar':
            self.row_usb.set_subtitle('Acepta "¿Permitir depuración USB?" en la tablet')
        elif adb_st == 'no_instalado':
            self.row_usb.set_subtitle('ADB no encontrado')
        else:
            self.row_usb.set_subtitle('Sin tablet USB detectada (activa Depuración USB)')

        ts_st, ts_ip = estado_tailscale()
        wg_st, wg_ip = estado_wireguard()
        self.row_ts.set_subtitle(f'Conectado ({ts_ip})' if ts_st == 'conectado' else ('Instalado, sin conectar' if ts_st == 'desconectado' else 'No instalado'))
        self.row_wg.set_subtitle(f'Conectado ({wg_ip})' if wg_st == 'conectado' else ('Listo (/etc/wireguard/wg0.conf)' if wg_st == 'configurado_inactivo' else 'Instalado (sin túnel wg0 activo)'))

        nets = detectar_redes()
        items = []
        if usb_ready:
            items.append((f'Cable USB (ADB): {proto}://127.0.0.1:{self.server.port}', f'{proto}://127.0.0.1:{self.server.port}'))
        for iface, ip in nets['lan']:
            lbl = 'Hotspot Wi-Fi' if (hs_active and iface == hs_dev) else f'Red Local ({iface})'
            items.append((f'{lbl}: {proto}://{ip}:{self.server.port}', f'{proto}://{ip}:{self.server.port}'))
        for iface, ip in nets['wireguard']:
            items.append((f'WireGuard ({iface}): {proto}://{ip}:{self.server.port}', f'{proto}://{ip}:{self.server.port}'))
        for iface, ip in nets['tailscale']:
            items.append((f'Tailscale ({iface}): {proto}://{ip}:{self.server.port}', f'{proto}://{ip}:{self.server.port}'))
        if not items:
            items.append((f'Localhost: {proto}://127.0.0.1:{self.server.port}', f'{proto}://127.0.0.1:{self.server.port}'))

        self._url_list = [u for _, u in items]
        self.combo_net.set_model(Gtk.StringList.new([lbl for lbl, _ in items]))
        self.combo_net.set_selected(0)
        self._set_qr(self._url_list[0])

    def _on_copy_url(self, *_):
        if self._qr_url_actual:
            self.get_clipboard().set(self._qr_url_actual)
            self.banner.set_title(f'URL copiada: {self._qr_url_actual}')

    def _on_test_cursor(self, *_):
        if self.server.monitor and self.server.monitor.running:
            self.server.monitor.move_pointer_abs(0.5, 0.5)
            self.banner.set_title('Cursor movido al centro del 2º monitor')
        else:
            self.banner.set_title('Inicia primero el 2º Monitor')

    def _on_toggle_hotspot(self, *_):
        self.btn_hotspot.set_sensitive(False)
        def _worker():
            activo, _, _, _ = estado_hotspot()
            if activo:
                ok, msg = desactivar_hotspot()
            else:
                ok, msg = activar_hotspot()
            def _ui():
                self.btn_hotspot.set_sensitive(True)
                self.banner.set_title(msg if ok else f'Error Hotspot: {msg}')
                self._refresh_nets()
                return False
            GLib.idle_add(_ui)
        threading.Thread(target=_worker, daemon=True).start()

    def _on_apk_qr(self, *_):
        if not self._url_list:
            return
        base = self._url_list[0].rstrip('/')
        apk_url = f'{base}/deskcast.apk'
        self._set_qr(apk_url)
        self.banner.set_title(f'Escanea este QR en tu tablet para descargar DeskCast.apk: {apk_url}')

    def _on_apk_install_usb(self, *_):
        apk_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'android', 'DeskCast.apk')
        adb = find_adb()
        if not adb or not os.path.isfile(apk_path):
            self.banner.set_title('Falta adb o android/DeskCast.apk')
            return
        def _worker():
            st, _ = estado_adb()
            if st != 'conectado':
                GLib.idle_add(lambda: (self.banner.set_title('Conecta la tablet por USB con Depuración USB para instalar DeskCast.apk'), False)[1])
                return
            r = subprocess.run([adb, 'install', '-r', apk_path], capture_output=True, text=True, timeout=30)
            msg = 'DeskCast.apk instalado en la tablet' if r.returncode == 0 else f'Error instalando APK: {r.stderr.strip() or r.stdout.strip()}'
            GLib.idle_add(lambda: (self.banner.set_title(msg), False)[1])
        threading.Thread(target=_worker, daemon=True).start()

    def _on_connect_usb(self, *_):
        def _worker():
            ok, msg = activar_adb_reverse(self.server.port)
            def _ui():
                if ok:
                    self.banner.set_title(f'Túnel USB activo · Abre en DeskCast Android o navegador: {msg}')
                    self._refresh_nets()
                else:
                    dlg = Adw.AlertDialog(
                        heading='Conectar tablet por Cable USB',
                        body=(
                            f'{msg}\n\n'
                            'Pasos en la tablet (solo la 1ª vez):\n'
                            '1. Ajustes → Información de la tablet → pulsa 7 veces "Número de compilación".\n'
                            '2. Ajustes → Sistema → Opciones de desarrollador → activa "Depuración por USB".\n'
                            '3. Conecta el cable USB y acepta "¿Permitir depuración USB?".\n'
                            f'4. Pulsa "Conectar por USB" y abre http://127.0.0.1:{self.server.port}.'
                        ),
                    )
                    dlg.add_response('ok', 'Entendido')
                    dlg.present(self)
                return False
            GLib.idle_add(_ui)
        threading.Thread(target=_worker, daemon=True).start()

    def _on_check_updates(self, *_):
        self.btn_update.set_sensitive(False)
        self.btn_update.set_label('↻ Comprobando...')
        def _worker():
            res = check_for_updates()
            def _ui():
                ver = get_local_version()
                self.btn_update.set_sensitive(True)
                self.btn_update.set_label(f'↻ Buscar actualizaciones (v{ver})')
                if not res.get('ok'):
                    dlg = Adw.AlertDialog(
                        heading='Comprobar actualizaciones en GitHub',
                        body=f"Versión instalada: v{res['local_version']}\nRepositorio: github.com/{res['repo']}\n\n{res['error']}",
                    )
                    dlg.add_response('ok', 'Cerrar')
                    dlg.present(self)
                    return False

                if res['has_update']:
                    notes = f"\n\nNovedades:\n{res['notes']}" if res.get('notes') else ''
                    dlg = Adw.AlertDialog(
                        heading=f"Nueva versión disponible: v{res['remote_version']}",
                        body=f"Versión actual: v{res['local_version']}\nRepositorio: github.com/{res['repo']}{notes}",
                    )
                    dlg.add_response('cancel', 'Más tarde')
                    dlg.add_response('update', 'Actualizar ahora')
                    dlg.set_response_appearance('update', Adw.ResponseAppearance.SUGGESTED)
                    dlg.connect('response', lambda _d, r: self._run_self_update(res.get('download_url')) if r == 'update' else None)
                    dlg.present(self)
                else:
                    dlg = Adw.AlertDialog(
                        heading='DeskCast está actualizado',
                        body=f"Tienes la última versión disponible (v{res['local_version']}) de github.com/{res['repo']}.",
                    )
                    dlg.add_response('ok', 'Genial')
                    dlg.present(self)
                return False
            GLib.idle_add(_ui)
        threading.Thread(target=_worker, daemon=True).start()

    def _run_self_update(self, download_url):
        self.btn_update.set_sensitive(False)
        self.banner.set_title('Descargando e instalando actualización desde GitHub...')
        def _worker():
            ok, msg = apply_update(download_url)
            def _ui():
                self.btn_update.set_sensitive(True)
                self.banner.set_title(msg)
                if ok:
                    dlg = Adw.AlertDialog(
                        heading='Actualización completada',
                        body=f'{msg}\n\n¿Quieres reiniciar DeskCast ahora?',
                    )
                    dlg.add_response('later', 'Luego')
                    dlg.add_response('restart', 'Reiniciar ahora')
                    dlg.set_response_appearance('restart', Adw.ResponseAppearance.SUGGESTED)
                    dlg.connect('response', lambda _d, r: self._restart_app() if r == 'restart' else None)
                    dlg.present(self)
                return False
            GLib.idle_add(_ui)
        threading.Thread(target=_worker, daemon=True).start()

    def _restart_app(self):
        self.server.stop()
        os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)])

    def _on_select_url(self, *_):
        idx = self.combo_net.get_selected()
        if 0 <= idx < len(self._url_list):
            self._set_qr(self._url_list[idx])

    def _set_qr(self, url):
        if url == self._qr_url_actual:
            return
        self._qr_url_actual = url
        self.lbl_url.set_text(url)
        try:
            png = qr_to_png_bytes(url, box=6, border=2)
            tex = Gdk.Texture.new_from_bytes(GLib.Bytes.new(png))
            self.qr_pic.set_paintable(tex)
        except Exception:
            pass

    def _tick_stats(self):
        kbps = self.server.kbps
        bw = f'{kbps / 1000:.1f} Mbps' if kbps > 1000 else f'{kbps} kbps'
        clients = [c for c in list(self.server.clients) if not c.closed]
        self.lbl_stats.set_text(
            f'Tablets: {len(clients)} · {self.server.actual_fps} FPS · {bw}'
        )
        if clients:
            ips = ', '.join(str(c.peer[0]) for c in clients if c.peer)
            self.lbl_peers.set_text(f'Conectada: {ips}')
        else:
            self.lbl_peers.set_text('Ninguna tablet conectada')
        return True

    def _on_setup_tailscale(self, *_):
        cmd = comando_instalar_tailscale()
        dlg = Adw.AlertDialog(
            heading='Conectar fuera de red con Tailscale',
            body=(
                '1. En este PC ejecuta en una terminal:\n'
                f'   {cmd}\n\n'
                '2. Instala la app gratuita "Tailscale" en tu tablet (Play Store / App Store) '
                'e inicia sesión con la misma cuenta.\n\n'
                '3. Pulsa "↻ Actualizar IPs y VPN" y escanea la URL 100.x.y.z desde cualquier red o 4G.'
            ),
        )
        dlg.add_response('copy', 'Copiar comando')
        dlg.add_response('ok', 'Cerrar')
        dlg.connect('response', lambda _d, r: self.get_clipboard().set(cmd) if r == 'copy' else None)
        dlg.present(self)

    def _on_info_wireguard(self, *_):
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'scripts', 'setup_wireguard_aws.sh')
        dlg = Adw.AlertDialog(
            heading='Túnel WireGuard propio',
            body=(
                'Si prefieres WireGuard puro sin cuentas externas, puedes usar un servidor VPS '
                'como punto de encuentro entre tu PC Linux y tu tablet.\n\n'
                f'Ejecuta en terminal:\n  AWS_HOST=tu-servidor.com bash "{script}"'
            ),
        )
        dlg.add_response('ok', 'Entendido')
        dlg.present(self)


class DeskCastApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id='io.github.davidlpizano.DeskCast', flags=Gio.ApplicationFlags.FLAGS_NONE)

    def do_activate(self):
        win = self.props.active_window or DeskCastWindow(self)
        win.present()
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, lambda: (win.server.stop(), self.quit(), False)[2])
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, lambda: (win.server.stop(), self.quit(), False)[2])


if __name__ == '__main__':
    app = DeskCastApp()
    sys.exit(app.run(sys.argv))
