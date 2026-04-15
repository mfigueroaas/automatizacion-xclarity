FROM mcr.microsoft.com/playwright/python:v1.58.0-jammy

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    openfortivpn \
    iproute2 \
    dos2unix \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# --- EL CAMBIO ESTÁ AQUÍ ---
COPY *.py ./
COPY start.sh ./

RUN dos2unix /app/start.sh && chmod +x /app/start.sh

CMD ["/app/start.sh"]