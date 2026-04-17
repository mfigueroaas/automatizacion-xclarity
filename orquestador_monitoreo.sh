#!/bin/bash
REPO_DIR="/root/monitoreo_lab"
echo "=== INICIANDO MONITOREO GENERAL - $(date) ==="

# FASE 1: XClarity (Hardware Físico)
docker run --rm --privileged --cap-add=NET_ADMIN --dns=8.8.8.8 \
  --device=/dev/net/tun --device=/dev/ppp \
  --env-file "$REPO_DIR/data/.env" -v "$REPO_DIR/data:/app/data" \
  -v /etc/localtime:/etc/localtime:ro -e TZ=America/Santiago \
  -e SCRIPT_A_EJECUTAR="script.py" monitor-vpn

echo "-> Fase 1 Terminada. Esperando 15s..."
sleep 15

# FASE 2: vCenter (Entorno Virtualizado)
docker run --rm --privileged --cap-add=NET_ADMIN --dns=8.8.8.8 \
  --device=/dev/net/tun --device=/dev/ppp \
  --env-file "$REPO_DIR/data/.env" -v "$REPO_DIR/data:/app/data" \
  -v /etc/localtime:/etc/localtime:ro -e TZ=America/Santiago \
  -e SCRIPT_A_EJECUTAR="script_vcenter.py" monitor-vpn

echo "-> Fase 2 Terminada. Esperando 15s..."
sleep 15

# FASE 3: ESXi Directo (Gestión de Hosts)
docker run --rm --privileged --cap-add=NET_ADMIN --dns=8.8.8.8 \
  --device=/dev/net/tun --device=/dev/ppp \
  --env-file "$REPO_DIR/data/.env" -v "$REPO_DIR/data:/app/data" \
  -v /etc/localtime:/etc/localtime:ro -e TZ=America/Santiago \
  -e SCRIPT_A_EJECUTAR="script_esxi.py" monitor-vpn

echo "=== CICLO DE MONITOREO FINALIZADO ==="