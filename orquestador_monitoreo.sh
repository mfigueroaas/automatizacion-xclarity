#!/bin/bash
# =========================================================
# Script Lanzador para el Host LXC (Proxmox)
# =========================================================

# Esta variable detecta automáticamente en qué carpeta clonaste el repo
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"

echo "=== INICIANDO MONITOREO GENERAL - $(date) ==="

echo "-> Fase 1: Escaneando Hardware (XClarity)..."
docker run --rm \
  --privileged \
  --cap-add=NET_ADMIN \
  --dns=8.8.8.8 \
  --device=/dev/net/tun \
  --device=/dev/ppp \
  --env-file "$REPO_DIR/data/.env" \
  -v "$REPO_DIR/data:/app/data" \
  -v /etc/localtime:/etc/localtime:ro \
  -e SCRIPT_A_EJECUTAR="script.py" \
  monitor-vpn

echo "-> Fase 2: Escaneando Virtualizacion (vCenter)..."
docker run --rm \
  --privileged \
  --cap-add=NET_ADMIN \
  --dns=8.8.8.8 \
  --device=/dev/net/tun \
  --device=/dev/ppp \
  --env-file "$REPO_DIR/data/.env" \
  -v "$REPO_DIR/data:/app/data" \
  -v /etc/localtime:/etc/localtime:ro \
  -e SCRIPT_A_EJECUTAR="script_vcenter.py" \
  monitor-vpn

echo "=== FIN DEL MONITOREO ==="