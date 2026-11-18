# DeskCast v1.0.3 · Usa tu tablet como 2º monitor en Linux (GNOME Wayland)

**DeskCast** convierte tu tablet Android (o cualquier navegador) en un **segundo monitor extendido real** en Linux (GNOME Wayland) a **60 FPS**, sin conectores HDMI falsos y con control táctil, ratón y teclado.

Funciona por **Hotspot Wi-Fi directo (5 GHz)**, **cable USB**, **red local** o **internet (Tailscale / WireGuard)**, usando codificación **H.264 por hardware** en la app nativa de Android (`DeskCast.apk`) o modo web universal.

---

## Descarga oficial (Última versión)

Ve a la sección **[Releases](https://github.com/davidlpizano/deskcast/releases/latest)** y descarga el archivo según tu dispositivo:

| Dispositivo | Archivo a descargar | Qué hace |
| :--- | :--- | :--- |
| **PC Linux (Ubuntu / Debian)** | `deskcast_<version>_amd64.deb` | Instala DeskCast y todas sus dependencias en tu sistema |
| **PC Linux (otra distro / portátil)** | `deskcast-linux-v<version>.tar.gz` | Carpeta lista para ejecutar con `./instalar.sh` |
| **Tablet / Móvil Android (8.0+)** | `DeskCast.apk` | App nativa ultra fluida con decodificación H.264 por hardware |

*(Si prefieres usar la tablet **sin instalar ninguna app**, también puedes conectarte abriendo la URL de DeskCast desde Chrome/Brave).*

---

## Guía paso a paso: qué tiene que hacer cada persona

### Paso 1 · Instalar en el PC Linux

#### Opción A — Paquete `.deb` en Ubuntu / Debian (la más fácil)
```bash
sudo apt update
sudo apt install ./deskcast_1.0.3_amd64.deb
```
Después abre **DeskCast** desde tu menú de aplicaciones (o ejecuta `deskcast` en la terminal).

#### Opción B — Desde el `.tar.gz` o clonando el repositorio
1. Instala las dependencias base:
   ```bash
   sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 python3-pil \
     gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-ugly \
     gcc openssl adb
   ```
2. Descarga y ejecuta el instalador:
   ```bash
   git clone https://github.com/davidlpizano/deskcast.git
   cd deskcast
   ./instalar.sh
   deskcast
   ```

---

### Paso 2 · Instalar la app en tu tablet Android

Tienes **3 formas** de poner `DeskCast.apk` en tu tablet (elige la más cómoda):
1. **Desde el navegador de la tablet (por Wi-Fi / Hotspot):**
   * Inicia DeskCast en el PC y pulsa **▶ Iniciar 2º Monitor**.
   * En la ventana del PC pulsa **QR Descarga APK**, escanéalo con la tablet, descarga `DeskCast.apk` e instálalo (acepta *"Instalar apps desconocidas"* si Android te lo pide).
2. **Por cable USB en 1 clic:**
   * Activa *Depuración por USB* en la tablet, conéctala por cable y pulsa **Instalar por USB** en DeskCast del PC.
3. **Descarga manual:** descarga `DeskCast.apk` desde [Releases](https://github.com/davidlpizano/deskcast/releases/latest) directamente en tu tablet y ábrelo.

---

### Paso 3 · Conectar PC y tablet (elige tu situación)

#### Opción 1 · Hotspot Wi-Fi directo del PC (recomendada si el Wi-Fi de clase/oficina bloquea dispositivos)
*No consume tus datos ni necesita el mismo Wi-Fi del aula.*
1. En el PC (DeskCast), pulsa **Encender Hotspot**. Se creará la red **`DeskCast-PC`** (clave por defecto: `DeskCast123`).
2. Conecta tu tablet al Wi-Fi **`DeskCast-PC`**.
3. Pulsa **▶ Iniciar 2º Monitor** en el PC.
4. Abre la app **DeskCast** en la tablet, toca **`Hotspot 10.42.0.1`** y pulsa **Conectar**.

#### Opción 2 · Cable USB (cero interferencias Wi-Fi)
1. **Solo la primera vez en Android:**
   * Ve a **Ajustes → Información de la tablet** y pulsa **7 veces** en *Número de compilación*.
   * Entra en **Ajustes → Sistema → Opciones de desarrollador** y activa **Depuración por USB**.
2. Enchufa el cable USB al PC y acepta **"¿Permitir depuración USB?"** en la pantalla de la tablet.
3. En DeskCast del PC pulsa **Conectar por USB** y **▶ Iniciar 2º Monitor**.
4. En la app **DeskCast** de la tablet toca **`USB 127.0.0.1`** y pulsa **Conectar**.

> **Truco Pro (Anclaje USB < 1 ms):** Si activas *Compartir conexión por USB / Anclaje a red USB* en Android y pulsas **↻ Actualizar IPs y VPN** en el PC, DeskCast evita automáticamente que Ubuntu pierda su Internet por cable y crea un enlace USB directo del kernel (`< 1 ms` de latencia).

#### Opción 3 · Misma red Wi-Fi de casa
1. Conecta PC y tablet al mismo Wi-Fi.
2. En DeskCast del PC pulsa **▶ Iniciar 2º Monitor**, selecciona **Red Local** en el desplegable y copia esa IP en la app de la tablet (o escanea el QR desde el navegador).

#### Opción 4 · Desde otro sitio por 4G/5G (Tailscale o WireGuard)
1. Instala **Tailscale** en el PC (`curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up`) y en la tablet con la misma cuenta.
2. En la app de la tablet toca **`Tailscale`**, escribe la IP `100.x.y.z` de tu PC y pulsa **Conectar**.

---

## Cómo usar el 2º monitor
* **Mover ventanas a la tablet:** arrastra cualquier ventana hacia el borde derecho de tu monitor principal (puedes cambiar la posición a Izquierda, Arriba, Abajo o Modo Espejo desde DeskCast).
* **Gestos en la app Android:**
  * **1 dedo:** mover cursor / clic izquierdo / arrastrar ventanas.
  * **2 dedos arriba/abajo:** hacer scroll.
  * **Botón `⌨` (arriba derecha):** abrir el teclado móvil para escribir en Linux.
  * **Botón `⚙` (arriba derecha):** cambiar resolución (`1280×800`, `1600×1000`, `1920×1200`), bitrate (`2500–10000 kbps`), modo Trackpad o Clic derecho.
* **Perfiles rápidos en el PC:**
  * **⚡ Ultra Fluido:** `1280×800 · 60 FPS · H.264 6000 kbps` (ideal uso diario y vídeo).
  * **🎨 Nítido / Lectura:** `1920×1200 · 30 FPS · H.264 8000 kbps` (ideal código y PDFs).
  * **📱 Ahorro 4G / Remoto:** `1024×640 · 25 FPS · Turbo Tiles` (bajo consumo en red móvil).
* **Actualizaciones automáticas:** pulsa **`↻ Buscar actualizaciones`** en la barra superior de DeskCast para comprobar e instalar nuevas versiones desde GitHub.

---

## Modo consola (sin ventana gráfica)
```bash
deskcast-cli --width 1280 --height 800 --position right --fps 60 --codec h264 --bitrate 6000
```

## Licencia
Código abierto bajo licencia **MIT** — consulta [LICENSE](LICENSE).
