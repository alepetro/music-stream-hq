FROM python:3.11-slim

# Install system dependencies (ffmpeg + node for yt-dlp JS challenges)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    nodejs \
    npm \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy the rest of the project
COPY . .

# Create assets directory if it doesn't exist
RUN mkdir -p assets

CMD ["python", "bot.py"]
