FROM python:3.12.14-alpine3.24
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN apk upgrade --no-cache \
    && pip install --no-cache-dir -r requirements.txt \
    && adduser -D -u 10001 app
COPY . .
RUN DEBUG=true python manage.py collectstatic --noinput && chown -R app:app /app
USER app
EXPOSE 8000
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "2", "--timeout", "120", "--access-logfile", "-", "--error-logfile", "-"]
