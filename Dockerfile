FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

RUN useradd --create-home --uid 1000 app \
    && mkdir -p /data/media /app/staticfiles \
    && chown -R app:app /data /app/staticfiles \
    && chmod +x /app/entrypoint.sh
USER app

RUN DJANGO_SECRET_KEY=build-only python manage.py collectstatic --noinput

EXPOSE 8000
ENTRYPOINT ["/app/entrypoint.sh"]
# gthread workers so slow 50 MB uploads don't block the whole server; generous timeout for the same reason.
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--worker-class", "gthread", "--workers", "2", "--threads", "4", "--timeout", "300", "--access-logfile", "-"]
