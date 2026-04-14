#!/usr/bin/env bash
set -euo pipefail

echo "--- Iniciando tunel VPN de Fortinet ---"

VPN_HOST="${VPN_HOST:-}"
VPN_PORT="${VPN_PORT:-443}"
VPN_USER="${VPN_USER:-}"
VPN_PASS="${VPN_PASS:-}"
VPN_CERT="${VPN_CERT:-}"

if [ -z "$VPN_HOST" ] || [ -z "$VPN_USER" ] || [ -z "$VPN_PASS" ] || [ -z "$VPN_CERT" ]; then
  echo "ERROR: faltan variables VPN_HOST, VPN_USER, VPN_PASS o VPN_CERT"
  exit 1
fi

# En LXC normalmente se monta /app/data/credenciales.json.
if [ -f /app/data/credenciales.json ] && [ ! -e /app/credenciales.json ]; then
  ln -s /app/data/credenciales.json /app/credenciales.json
fi

openfortivpn "${VPN_HOST}:${VPN_PORT}" \
  -u "$VPN_USER" \
  -p "$VPN_PASS" \
  --trusted-cert "$VPN_CERT" \
  --set-dns=0 &
VPN_PID=$!

cleanup() {
  if kill -0 "$VPN_PID" >/dev/null 2>&1; then
    kill "$VPN_PID" || true
  fi
}
trap cleanup EXIT

echo "Esperando conexion de ppp0..."
for i in $(seq 1 15); do
  if ip addr show ppp0 >/dev/null 2>&1; then
    echo "VPN conectada exitosamente (interfaz ppp0 activa)."
    break
  fi
  sleep 2
done

if ! ip addr show ppp0 >/dev/null 2>&1; then
  echo "ERROR: no se pudo establecer la conexion VPN (ppp0 no disponible)."
  exit 1
fi

echo "nameserver 8.8.8.8" > /etc/resolv.conf
echo "DNS configurado para salida a Google Sheets."

echo "--- Iniciando script de monitoreo XClarity ---"
exec python /app/script.py
