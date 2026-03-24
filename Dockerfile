FROM python:3.11-slim

# Instalar fuentes Liberation (equivalente a Arial/Helvetica)
RUN apt-get update && apt-get install -y \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=5000

CMD gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120
