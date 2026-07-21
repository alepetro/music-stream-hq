FROM python:3.10-slim

# Installa FFmpeg
RUN apt-get update && apt-get install -y \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Imposta prima la directory di lavoro
WORKDIR /app

# Ora copia i requirements e installali dentro /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia tutto il resto del codice
COPY . .

# Avvia il bot
CMD ["python", "bot.py"]
