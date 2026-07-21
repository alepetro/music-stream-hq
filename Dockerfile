FROM python:3.10-slim

RUN apt-get update && apt-get install -y \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY . /app/

# Installa direttamente le librerie a mano nel container senza passare dal file txt
RUN pip install --no-cache-dir pyTelegramBotAPI yt-dlp shazamio requests

CMD ["python", "bot.py"]
