FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SMART_WATERING_DB_PATH=/data/smart_watering.db \
    TMPDIR=/data

WORKDIR /app

COPY smart_watering/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY smart_watering ./smart_watering

RUN mkdir -p /data

VOLUME ["/data"]

