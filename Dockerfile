FROM python:3.12-slim

WORKDIR /app

# Instala dependencias primero (mejor cache de capas).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia el resto del proyecto (los negocio_*.txt y .csv viajan en la imagen).
COPY . .

ENV PORT=8080

# 1 worker porque el estado (SESSIONS, HUMAN_MODE) vive en memoria del proceso:
# varios workers partirían las conversaciones. --threads para atender varios
# webhooks a la vez. --timeout alto por si el modelo tarda.
CMD exec gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 120 main:app
