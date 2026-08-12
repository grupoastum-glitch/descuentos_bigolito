# Empaqueta solo lo que el bot necesita para correr: su código y el
# recorte de datos que lee desde ../web/data (misma fuente que la web).
# El resto de web/ (html, js, css, imágenes) no entra en esta imagen.
FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY bot/requirements.txt bot/requirements.txt
RUN pip install --no-cache-dir -r bot/requirements.txt

COPY bot/bot.py bot/bot.py
COPY web/data/ web/data/

WORKDIR /app/bot
CMD ["python", "bot.py"]
