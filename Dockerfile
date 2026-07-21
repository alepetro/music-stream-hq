FROM python:3.10-slim

# Installa FFmpeg
RUN apt-get update && apt-get install -y \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copia esplicitamente il file requirements.txt dalla root del repository
COPY requirements.txt /app/

RUN pip install --no-cache-dir -r requirements.txt

# Copia il resto del codice
COPY . /app/

CMD ["python", "bot.py"]
