"""Detección de interfaces de red (LAN, Tailscale, WireGuard) y utilidades de acceso remoto."""
import os
import shutil
import socket
import subprocess


def _run(cmd, timeout=3):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as exc:
        return 1, '', str(exc)


def detectar_redes():
    """Devuelve dict con ips locales, tailscale y wireguard."""
    lan = []
    tailscale = []
    wireguard = []

    rc, out, _ = _run(['ip', '-o', '-4', 'addr', 'show', 'up'])
    if rc == 0 and out:
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue
            iface = parts[1]
            ip = parts[3].split('/')[0]
            if ip.startswith('127.') or iface in ('lo', 'docker0') or iface.startswith(('veth', 'br-')):
                continue
            if iface.startswith('tailscale') or ip.startswith('100.'):
                tailscale.append((iface, ip))
            elif iface.startswith('wg'):
                wireguard.append((iface, ip))
            else:
                lan.append((iface, ip))

    if not lan and not tailscale and not wireguard:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(('8.8.8.8', 80))
            lan.append(('default', s.getsockname()[0]))
            s.close()
        except Exception:
            pass

    return {
        'lan': lan,
        'tailscale': tailscale,
        'wireguard': wireguard,
        'tailscale_installed': shutil.which('tailscale') is not None,
        'wg_installed': shutil.which('wg') is not None,
    }


def estado_tailscale():
    if not shutil.which('tailscale'):
        return 'no_instalado', None
    rc, out, err = _run(['tailscale', 'ip', '-4'])
    if rc == 0 and out:
        return 'conectado', out.splitlines()[0].strip()
    return 'desconectado', (err or out or 'Ejecuta: sudo tailscale up')


def estado_wireguard():
    if not shutil.which('wg'):
        return 'no_instalado', None
    rc, out, _ = _run(['ip', '-o', '-4', 'addr', 'show', 'wg0'])
    if rc == 0 and out:
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 4:
                return 'conectado', parts[3].split('/')[0]
    if os.path.exists('/etc/wireguard/wg0.conf'):
        return 'configurado_inactivo', None
    return 'sin_configurar', None


def comando_instalar_tailscale():
    return 'curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up'


def find_adb():
    """Localiza el binario adb en PATH o en ~/.local."""
    candidates = [
        shutil.which('adb'),
        os.path.expanduser('~/.local/bin/adb'),
        os.path.expanduser('~/.local/share/android-platform-tools/platform-tools/adb'),
        '/usr/bin/adb',
    ]
    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def estado_adb():
    """Devuelve (estado, detalle) para conexión USB por ADB."""
    adb = find_adb()
    if not adb:
        return 'no_instalado', None
    rc, out, err = _run([adb, 'devices'], timeout=4)
    if rc != 0:
        return 'error', err or out
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        if state == 'device':
            return 'conectado', serial
        if state == 'unauthorized':
            return 'sin_autorizar', serial
        return state, serial
    return 'sin_dispositivo', None


def activar_adb_reverse(port=8765):
    """Activa túnel USB `adb reverse tcp:<port> tcp:<port>`. Devuelve (ok, msg)."""
    adb = find_adb()
    if not adb:
        return False, 'adb no está instalado en el sistema'
    st, detail = estado_adb()
    if st == 'sin_autorizar':
        return False, f'Acepta "¿Permitir depuración USB?" en tu tablet ({detail}) y vuelve a pulsar'
    if st != 'conectado':
        return False, 'No se detecta tablet por USB con "Depuración USB" activada'
    rc, out, err = _run([adb, 'reverse', f'tcp:{int(port)}', f'tcp:{int(port)}'], timeout=5)
    if rc == 0:
        return True, f'http://127.0.0.1:{int(port)}'
    return False, (err or out or 'Error ejecutando adb reverse')


def find_wifi_iface():
    """Devuelve el nombre de la interfaz Wi-Fi (ej. wlp1s0) o None."""
    if not shutil.which('nmcli'):
        return None
    rc, out, _ = _run(['nmcli', '-t', '-f', 'DEVICE,TYPE', 'device', 'status'], timeout=3)
    if rc == 0:
        for line in out.splitlines():
            parts = line.split(':')
            if len(parts) >= 2 and parts[1] == 'wifi':
                return parts[0]
    return None


def estado_hotspot():
    """Devuelve (activo: bool, iface, ip, ssid)."""
    if not shutil.which('nmcli'):
        return False, None, None, None
    rc, out, _ = _run(['nmcli', '-t', '-f', 'NAME,TYPE,DEVICE', 'connection', 'show', '--active'], timeout=3)
    if rc == 0:
        for line in out.splitlines():
            parts = line.split(':')
            if len(parts) >= 3 and parts[1] == '802-11-wireless':
                name, _, dev = parts[0], parts[1], parts[2]
                # Comprobar si está en modo AP
                rc2, mode_out, _ = _run(['nmcli', '-t', '-f', '802-11-wireless.mode,802-11-wireless.ssid', 'connection', 'show', name], timeout=3)
                if rc2 == 0 and 'ap' in mode_out.lower():
                    ssid = ''
                    for ml in mode_out.splitlines():
                        if ml.startswith('802-11-wireless.ssid:'):
                            ssid = ml.split(':', 1)[1]
                    rc3, ip_out, _ = _run(['ip', '-o', '-4', 'addr', 'show', dev], timeout=2)
                    ip = None
                    if rc3 == 0 and ip_out:
                        ip = ip_out.split()[3].split('/')[0]
                    return True, dev, ip, ssid
    return False, find_wifi_iface(), None, None


def activar_hotspot(ssid='DeskCast-PC', password='DeskCast123', band='a'):
    """Crea/activa un punto de acceso Wi-Fi en la antena del PC. Devuelve (ok, msg)."""
    iface = find_wifi_iface()
    if not iface:
        return False, 'No se detectó interfaz Wi-Fi'
    cmd = ['nmcli', 'device', 'wifi', 'hotspot', 'ifname', iface, 'ssid', ssid, 'password', password]
    if band in ('a', 'bg'):
        cmd.extend(['band', band])
    rc, out, err = _run(cmd, timeout=12)
    if rc != 0 and band == 'a':
        # Reintentar en 2.4 GHz si el driver/canal 5 GHz falla
        rc, out, err = _run(['nmcli', 'device', 'wifi', 'hotspot', 'ifname', iface, 'band', 'bg', 'ssid', ssid, 'password', password], timeout=12)
    if rc == 0:
        _, _, ip, active_ssid = estado_hotspot()
        return True, f'Red: {active_ssid or ssid} · Clave: {password} · IP: {ip or "10.42.0.1"}'
    return False, (err or out or 'No se pudo iniciar el Hotspot Wi-Fi')


def desactivar_hotspot():
    """Desactiva el Hotspot Wi-Fi activo."""
    activo, _, _, _ = estado_hotspot()
    if not activo:
        return True, 'Hotspot apagado'
    rc, out, err = _run(['nmcli', 'connection', 'down', 'Hotspot'], timeout=6)
    return rc == 0, (err or out or 'Hotspot apagado')
