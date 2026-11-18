#!/usr/bin/env bash
set -euo pipefail
rm -f "$HOME/.local/bin/deskcast" \
      "$HOME/.local/bin/deskcast-cli" \
      "$HOME/.local/share/applications/deskcast.desktop" \
      "$HOME/.local/share/icons/hicolor/scalable/apps/deskcast.svg"
update-desktop-database "$HOME/.local/share/applications" >/dev/null 2>&1 || true
echo "✅ Acceso directo y comandos de DeskCast eliminados."
