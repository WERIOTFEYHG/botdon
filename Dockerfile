FROM python:3.12-slim

# نصب ffmpeg و وابستگی‌های ضروری
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# اول requirements.txt رو کپی کن (برای استفاده بهتر از کش Docker)
COPY requirements.txt .

# نصب پکیج‌های پایتون
RUN pip install --no-cache-dir -r requirements.txt

# بعد بقیه فایل‌ها رو کپی کن
COPY . .

# ساخت دایرکتوری‌های ضروری (برای Railway Volume هم مناسبه)
RUN mkdir -p /tmp/downloads /data

ENV PYTHONUNBUFFERED=1
ENV DOWNLOAD_DIR=/tmp/downloads
ENV DB_PATH=/data/bot_data.db

# اجرای ربات
CMD ["python", "main.py"]
