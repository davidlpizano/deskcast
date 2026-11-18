#!/usr/bin/env bash
# Genera claves y configuración WireGuard Hub (servidor externo) + Peer Linux + Peer Tablet
# para usar DeskCast desde cualquier red sin depender de terceros.
set -euo pipefail

OUT_DIR="${1:-$HOME/deskcast/wireguard-keys}"
AWS_HOST="${AWS_HOST:-tu-servidor-publico.example.com}"
WG_PORT="${WG_PORT:-51820}"

command -v wg >/dev/null || { echo "Falta 'wg'. Instala wireguard-tools."; exit 1; }

mkdir -p "$OUT_DIR"
chmod 700 "$OUT_DIR"

umask 077
wg genkey | tee "$OUT_DIR/aws_priv" | wg pubkey > "$OUT_DIR/aws_pub"
wg genkey | tee "$OUT_DIR/pc_priv"  | wg pubkey > "$OUT_DIR/pc_pub"
wg genkey | tee "$OUT_DIR/tab_priv" | wg pubkey > "$OUT_DIR/tab_pub"

AWS_PRIV=$(cat "$OUT_DIR/aws_priv")
AWS_PUB=$(cat "$OUT_DIR/aws_pub")
PC_PRIV=$(cat "$OUT_DIR/pc_priv")
PC_PUB=$(cat "$OUT_DIR/pc_pub")
TAB_PRIV=$(cat "$OUT_DIR/tab_priv")
TAB_PUB=$(cat "$OUT_DIR/tab_pub")

cat > "$OUT_DIR/aws-wg0.conf" <<EOF
[Interface]
Address = 10.66.0.1/24
ListenPort = ${WG_PORT}
PrivateKey = ${AWS_PRIV}

# PC Linux
[Peer]
PublicKey = ${PC_PUB}
AllowedIPs = 10.66.0.2/32

# Tablet
[Peer]
PublicKey = ${TAB_PUB}
AllowedIPs = 10.66.0.3/32
EOF

cat > "$OUT_DIR/pc-wg0.conf" <<EOF
[Interface]
Address = 10.66.0.2/24
PrivateKey = ${PC_PRIV}

[Peer]
PublicKey = ${AWS_PUB}
Endpoint = ${AWS_HOST}:${WG_PORT}
AllowedIPs = 10.66.0.0/24
PersistentKeepalive = 20
EOF

cat > "$OUT_DIR/tablet-wg0.conf" <<EOF
[Interface]
Address = 10.66.0.3/24
PrivateKey = ${TAB_PRIV}

[Peer]
PublicKey = ${AWS_PUB}
Endpoint = ${AWS_HOST}:${WG_PORT}
AllowedIPs = 10.66.0.0/24
PersistentKeepalive = 20
EOF

echo "✅ Archivos WireGuard generados en: $OUT_DIR"
echo "1) En AWS: abre UDP ${WG_PORT} en el Security Group, instala wireguard y copia aws-wg0.conf a /etc/wireguard/wg0.conf (activa net.ipv4.ip_forward=1)."
echo "2) En este PC: sudo cp $OUT_DIR/pc-wg0.conf /etc/wireguard/wg0.conf && sudo wg-quick up wg0"
echo "3) En tu tablet: importa tablet-wg0.conf en la app oficial de WireGuard y abre http://10.66.0.2:8765"
