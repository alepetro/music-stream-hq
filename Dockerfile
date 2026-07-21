FROM python:3.10-slim

RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copia esplicita di tutto il contenuto della cartella corrente
COPY . /app/

RUN pip install --no-cache-dir -r requirements.txt

CMD ["python", "bot.py"]
