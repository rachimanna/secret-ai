# Secret AI on any Linux server (VPS). Render doesn't use this file; deploy/install.sh does.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 DATABASE_PATH=/data/memory.db PORT=8080 KEEP_AWAKE=0
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
VOLUME /data
CMD ["python", "bot.py"]
