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