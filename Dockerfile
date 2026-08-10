FROM python:3.12-slim

# نصب ffmpeg و وابستگی‌ها
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

RUN mkdir -p /tmp/downloads /data

ENV PYTHONUNBUFFERED=1
ENV DOWNLOAD_DIR=/tmp/downloads
ENV DB_PATH=/data/bot_data.db

CMD ["python", "main.py"]
