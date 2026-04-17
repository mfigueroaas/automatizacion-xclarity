#!/usr/bin/env bash
set -euo pipefail

echo "--- Iniciando tunel VPN de Fortinet ---"

VPN_HOST="${VPN_HOST:-}"
VPN_PORT="${VPN_PORT:-}"
VPN_USER="${VPN_USER:-}"
VPN_PASS="${VPN_PASS:-}"
VPN_CERT="${VPN_CERT:-}"

if [ -z "$VPN_HOST" ] || [ -z "$VPN_USER" ] || [ -z "$VPN_PASS" ] || [ -z "$VPN_CERT" ]; then
  echo "ERROR: faltan variables VPN_HOST, VPN_USER, VPN_PASS o VPN_CERT"
  exit 1
fi

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

# --- CONFIGURACIÓN DE RED Y ENRUTAMIENTO ---

# Leer archivo .env manualmente para asegurar que Bash vea las variables
if [ -f "/app/data/.env" ]; then
    export $(grep -v '^#' /app/data/.env | xargs)
fi

# 1. Configuración de DNS
if [ -n "${DNS_EMPRESA:-}" ]; then
    echo "nameserver $DNS_EMPRESA" > /etc/resolv.conf
    echo "nameserver 8.8.8.8" >> /etc/resolv.conf
    echo "DNS Mixto configurado (Interno: $DNS_EMPRESA + Google)."
else
    # Si por alguna razón falla, volver al valor por defecto que funcionaba
    echo "nameserver 192.168.1.63" > /etc/resolv.conf
    echo "nameserver 8.8.8.8" >> /etc/resolv.conf
    echo "DNS Mixto configurado (Hardcodeado)."
fi

# 2. Configuración de Rutas Estáticas
if [ -n "${ESXI_IP_RUTA:-}" ]; then
    ip route add "$ESXI_IP_RUTA/32" dev ppp0
    echo "Ruta forzada hacia ESXi ($ESXI_IP_RUTA) a través de ppp0."
else
    # Si falla la variable, inyectar directamente la IP para no romper producción
    ip route add 192.168.1.4/32 dev ppp0
    echo "Ruta forzada hacia ESXi (192.168.1.4 - Hardcodeado) a través de ppp0."
fi
# ---------------------------------------------------

# --- EJECUCIÓN DEL SCRIPT ---
if [ -z "${SCRIPT_A_EJECUTAR:-}" ]; then
    echo "ERROR: No le dijiste a Docker qué script ejecutar."
    exit 1
else
    echo "--- Iniciando script: $SCRIPT_A_EJECUTAR ---"
    exec python "/app/$SCRIPT_A_EJECUTAR"
fi