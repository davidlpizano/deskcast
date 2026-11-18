#!/usr/bin/env bash
set -euo pipefail

AQUI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$HOME/.local/bin" "$HOME/.local/share/applications" "$HOME/.local/share/icons/hicolor/scalable/apps"

chmod +x "$AQUI/deskcast_app.py" "$AQUI/deskcast_server.py" "$AQUI/scripts/setup_wireguard_aws.sh" "$AQUI/desinstalar.sh"

if [ ! -f "$AQUI/fast_diff.so" ] && [ -f "$AQUI/fast_diff.c" ] && command -v gcc >/dev/null 2>&1; then
  gcc -O3 -shared -fPIC "$AQUI/fast_diff.c" -o "$AQUI/fast_diff.so"
fi

if [ -f "$AQUI/assets/deskcast.svg" ]; then
  cp "$AQUI/assets/deskcast.svg" "$HOME/.local/share/icons/hicolor/scalable/apps/deskcast.svg"
fi

cat > "$HOME/.local/bin/deskcast" <<EOF
#!/usr/bin/env bash
exec python3 "$AQUI/deskcast_app.py" "\$@"
EOF
chmod +x "$HOME/.local/bin/deskcast"

cat > "$HOME/.local/bin/deskcast-cli" <<EOF
#!/usr/bin/env bash
exec python3 "$AQUI/deskcast_server.py" "\$@"
EOF
chmod +x "$HOME/.local/bin/deskcast-cli"

cat > "$HOME/.local/share/applications/deskcast.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=DeskCast
Comment=Monitor virtual inalámbrico y USB de baja latencia para GNOME Wayland
Exec=$HOME/.local/bin/deskcast
Icon=deskcast
Terminal=false
Categories=Utility;Network;GTK;AudioVideo;
StartupNotify=true
EOF

update-desktop-database "$HOME/.local/share/applications" >/dev/null 2>&1 || true

echo "✅ DeskCast instalado."
echo "   - App gráfica: deskcast (o búscala en tu menú de aplicaciones)"
echo "   - Modo consola: deskcast-cli --width 1280 --height 800 --codec h264"
