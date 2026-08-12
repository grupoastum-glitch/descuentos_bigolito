FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY scraper/requirements.txt scraper/requirements.txt
RUN pip install --no-cache-dir -r scraper/requirements.txt
# Test diagnóstico: confirmar que Chromium headless corre en este contenedor (ver
# scraper/main.py, _diagnostico_headless). --with-deps instala las librerías de sistema
# que el navegador necesita; --no-shell evita instalar la variante headless-shell sin usar.
RUN patchright install --with-deps --no-shell chromium
COPY scraper/ scraper/

WORKDIR /app/scraper
CMD ["python", "main.py"]
