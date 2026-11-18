#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VER="$(tr -d ' \n\r' < "$ROOT/VERSION")"
DIST="$ROOT/dist"
rm -rf "$DIST"
mkdir -p "$DIST"

# 1. Compilar fast_diff.so
if [ -f "$ROOT/fast_diff.c" ] && command -v gcc >/dev/null 2>&1; then
  gcc -O3 -shared -fPIC "$ROOT/fast_diff.c" -o "$ROOT/fast_diff.so"
fi

# 2. Compilar APK Android
if [ -f "$ROOT/android/build.sh" ]; then
  bash "$ROOT/android/build.sh"
fi
cp "$ROOT/android/DeskCast.apk" "$DIST/DeskCast.apk"

# 3. Empaquetar .tar.gz portable para cualquier Linux
TAR_STAGE="$DIST/deskcast-$VER"
mkdir -p "$TAR_STAGE/android/src/com/deskcast/client" "$TAR_STAGE/android/res/values" "$TAR_STAGE/assets" "$TAR_STAGE/scripts" "$TAR_STAGE/web"
cp "$ROOT"/*.py "$ROOT/fast_diff.c" "$ROOT/instalar.sh" "$ROOT/desinstalar.sh" "$ROOT/README.md" "$ROOT/LICENSE" "$ROOT/VERSION" "$TAR_STAGE/"
cp "$ROOT/assets/deskcast.svg" "$TAR_STAGE/assets/"
cp "$ROOT/scripts/"*.sh "$TAR_STAGE/scripts/"
cp "$ROOT/web/"* "$TAR_STAGE/web/"
cp "$ROOT/android/DeskCast.apk" "$ROOT/android/AndroidManifest.xml" "$ROOT/android/build.sh" "$TAR_STAGE/android/"
cp "$ROOT/android/res/values/styles.xml" "$TAR_STAGE/android/res/values/"
cp "$ROOT/android/src/com/deskcast/client/"*.java "$TAR_STAGE/android/src/com/deskcast/client/"
(cd "$DIST" && tar -czf "deskcast-linux-v${VER}.tar.gz" "deskcast-$VER")
rm -rf "$TAR_STAGE"

# 4. Empaquetar .deb para Ubuntu / Debian (amd64)
DEB_ROOT="$DIST/deb_pkg"
mkdir -p "$DEB_ROOT/DEBIAN" "$DEB_ROOT/opt/deskcast/android" "$DEB_ROOT/opt/deskcast/assets" "$DEB_ROOT/opt/deskcast/scripts" "$DEB_ROOT/opt/deskcast/web"
mkdir -p "$DEB_ROOT/usr/bin" "$DEB_ROOT/usr/share/applications" "$DEB_ROOT/usr/share/icons/hicolor/scalable/apps"

cp "$ROOT"/*.py "$ROOT/fast_diff.c" "$ROOT/fast_diff.so" "$ROOT/README.md" "$ROOT/LICENSE" "$ROOT/VERSION" "$DEB_ROOT/opt/deskcast/"
cp "$ROOT/assets/deskcast.svg" "$DEB_ROOT/opt/deskcast/assets/"
cp "$ROOT/assets/deskcast.svg" "$DEB_ROOT/usr/share/icons/hicolor/scalable/apps/deskcast.svg"
cp "$ROOT/scripts/"*.sh "$DEB_ROOT/opt/deskcast/scripts/"
cp "$ROOT/web/"* "$DEB_ROOT/opt/deskcast/web/"
cp "$ROOT/android/DeskCast.apk" "$DEB_ROOT/opt/deskcast/android/"
chmod +x "$DEB_ROOT/opt/deskcast/deskcast_app.py" "$DEB_ROOT/opt/deskcast/deskcast_server.py"

cat > "$DEB_ROOT/usr/bin/deskcast" <<'EOF'
#!/usr/bin/env bash
exec python3 /opt/deskcast/deskcast_app.py "$@"
EOF
chmod +x "$DEB_ROOT/usr/bin/deskcast"

cat > "$DEB_ROOT/usr/bin/deskcast-cli" <<'EOF'
#!/usr/bin/env bash
exec python3 /opt/deskcast/deskcast_server.py "$@"
EOF
chmod +x "$DEB_ROOT/usr/bin/deskcast-cli"

cat > "$DEB_ROOT/usr/share/applications/deskcast.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=DeskCast
Comment=Monitor virtual inalámbrico y USB de baja latencia para GNOME Wayland
Exec=/usr/bin/deskcast
Icon=deskcast
Terminal=false
Categories=Utility;Network;GTK;AudioVideo;
StartupNotify=true
EOF

cat > "$DEB_ROOT/DEBIAN/control" <<EOF
Package: deskcast
Version: ${VER}
Section: utils
Priority: optional
Architecture: amd64
Maintainer: David Lopez Pizano <lopez.pizano.david@gmail.com>
Depends: python3, python3-gi, gir1.2-gtk-4.0, gir1.2-adw-1, python3-pil, gstreamer1.0-plugins-base, gstreamer1.0-plugins-good, gstreamer1.0-plugins-ugly, openssl
Recommends: adb, network-manager
Description: Virtual second monitor for GNOME Wayland (Android H.264 & Web)
 DeskCast turns any Android tablet or browser into a low-latency 60 FPS
 extended second display on GNOME Wayland via Mutter ScreenCast, PipeWire,
 GStreamer H.264, USB (ADB/Tethering), Wi-Fi Hotspot, and Tailscale/WireGuard.
EOF

dpkg-deb --build --root-owner-group "$DEB_ROOT" "$DIST/deskcast_${VER}_amd64.deb"
rm -rf "$DEB_ROOT"

ls -lh "$DIST"
