FROM python:3.11-slim

# Instalar fuentes Liberation (equivalente a Arial/Helvetica)
RUN apt-get update && apt-get install -y \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Asegurar que el logo esté presente
RUN test -f /app/logo_lared.png && echo "Logo OK" || (echo "ERROR: logo_lared.png no encontrado" && exit 1)

ENV PORT=5000

CMD gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120
