FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATABASE_PATH=/data/musicbot.sqlite3

# FFmpeg để giải mã PCM, libopus cho voice của Discord, ca-certificates cho HTTPS.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        libopus0 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt "yt-dlp[default]"

COPY . .

# Railway gắn volume vào /data; tạo sẵn để chạy được cả khi không có volume.
RUN mkdir -p /data

CMD ["python", "main.py"]
