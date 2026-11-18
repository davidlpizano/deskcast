# DeskCast · Segundo Monitor Virtual para GNOME Wayland (Linux + Android / Web)

**DeskCast** es una aplicación de código abierto (Licencia MIT) que convierte cualquier tablet o navegador en un **segundo monitor extendido real** para Linux (GNOME Wayland), con soporte **H.264 por hardware** en su app nativa Android (`DeskCast.apk`), **USB directo (`adb reverse`)**, **Hotspot Wi-Fi 5 GHz**, **Turbo Tiles (JPEG diferencial en C)** y túneles VPN (**Tailscale / WireGuard**).

## Características principales
* **Monitor virtual nativo en GNOME Wayland (`Mutter ScreenCast RecordVirtual`)**: crea una pantalla extendida (`Meta-0`) sin conectores HDMI falsos (configurable a Derecha, Izquierda, Arriba, Abajo o Modo Espejo).
* **Doble motor de streaming de baja latencia (`PipeWire + GStreamer`)**:
  * **H.264 (hasta 60 FPS)**: `videoconvert` multihilo + `x264enc` (`zerolatency`, `superfast`, VBV acotado) decodificado por hardware en la tablet (`MediaCodec` → `SurfaceView` en Android o WebCodecs en navegador).
  * **Turbo Tiles (C + JPEG)**: comparación de tiles `128×128` en C ([fast_diff.c](fast_diff.c)), compresión paralela en `ThreadPoolExecutor` y ventana deslizante de ACKs.
* **Múltiples vías de conexión**:
  * **Cable USB (`adb reverse`)**: `< 2 ms` de latencia sin depender de ninguna red Wi-Fi.
  * **Hotspot Wi-Fi directo**: punto de acceso en 5 GHz desde la antena del PC.
  * **Red Local / Tailscale / WireGuard**.
* **Entrada completa**: táctil directo, modo trackpad, clic derecho, scroll con 2 dedos y teclado.
* **Actualizaciones integradas desde GitHub**: botón **Buscar actualizaciones** en la propia app (`Releases` o rama `main`).

## Instalación rápida en Linux
```bash
git clone https://github.com/davidlpizano/deskcast.git
cd deskcast
./instalar.sh
deskcast
```

### Dependencias en Ubuntu / Debian
```bash
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 python3-pil \
  gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-ugly \
  gcc openssl
```
*(Opcional para cable USB: `adb`; para Hotspot: `network-manager` + `dnsmasq-base`).*

## Compilar la app nativa Android (`DeskCast.apk`)
```bash
bash android/build.sh
```
Genera `android/DeskCast.apk` (también descargable directamente desde el servidor de DeskCast en `/deskcast.apk` o instalable con un clic por USB).

## Modo consola (sin interfaz gráfica)
```bash
deskcast-cli --width 1280 --height 800 --position right --fps 60 --codec h264 --bitrate 6000
```

## Licencia
Distribuido bajo la **Licencia MIT**. Consulta [LICENSE](LICENSE).
