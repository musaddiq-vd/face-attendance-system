FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# All private data (database, photos, generated keys) lives in /data: mount a volume there
ENV HOST=0.0.0.0 \
    PORT=5000 \
    DATA_DIR=/data \
    PYTHONUNBUFFERED=1
RUN useradd --create-home appuser && mkdir /data && chown appuser /data
USER appuser
VOLUME /data
EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/healthz', timeout=4)"

CMD ["python", "app.py"]
