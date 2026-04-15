# Automatización de Monitoreo: XClarity & vCenter (VPN + Sheets)

Contenedor Docker efímero que levanta un túnel Fortinet y realiza web scraping (Playwright) para extraer métricas de salud de servidores físicos (Lenovo XClarity) y entornos virtuales (VMware vCenter), enviando los datos a Google Sheets.

## 🚀 Instalación desde Cero (Proxmox LXC)

**1. Clonar el repositorio y preparar datos:**
\`\`\`bash
git clone https://github.com/mfigueroaas/automatizacion-xclarity.git /root/monitoreo_lab
cd /root/monitoreo_lab
mkdir data
cp .env.example data/.env
# IMPORTANTE: Colocar credenciales.json dentro de la carpeta data/ y editar el .env
\`\`\`

**2. Dar permisos y construir la imagen:**
\`\`\`bash
chmod +x orquestador_monitoreo.sh
docker build -t monitor-vpn .
\`\`\`

**3. Automatizar (Cron):**
Ejecutar `crontab -e` y añadir la siguiente línea para ejecutar 1 vez por hora:
\`\`\`cron
0 * * * * /root/monitoreo_lab/orquestador_monitoreo.sh >> /root/log_monitoreo_general.txt 2>&1
\`\`\`

*Nota: Asegurarse de que el LXC tiene habilitados los dispositivos TUN/PPP en su archivo de configuración `.conf` en Proxmox.*