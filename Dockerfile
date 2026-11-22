FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    APP_ENV=production FLASK_APP=app TRUST_PROXY=1 COOKIE_SECURE=1 \
    DATABASE=/data/kadry.db UPLOAD_DIR=/data/uploads BACKUP_DIR=/data/backups
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd --system --uid 10001 app && mkdir -p /data && chown -R app /data && chmod +x deploy/entrypoint.sh
USER app
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"
CMD ["deploy/entrypoint.sh"]
