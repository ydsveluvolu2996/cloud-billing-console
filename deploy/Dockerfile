FROM python:3.14.7-alpine3.24
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN apk upgrade --no-cache \
    && pip install --no-cache-dir -r requirements.txt \
    && adduser -D -u 10001 app
COPY . .
# Package installers are build-only. Remove pip and its vendored code/SBOM,
# plus ensurepip's bundled installer wheel, from the deployed runtime.
# Application dependency SBOMs remain intact for vulnerability scanning.
RUN DEBUG=true python manage.py collectstatic --noinput \
    && python -m pip check \
    && python -m pip uninstall --yes pip \
    && python -c "import pathlib, shutil, sysconfig; shutil.rmtree(pathlib.Path(sysconfig.get_path('stdlib')) / 'ensurepip')" \
    && python -c "import importlib.util; assert importlib.util.find_spec('pip') is None; assert importlib.util.find_spec('ensurepip') is None" \
    && chown -R app:app /app
USER app
EXPOSE 8000
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "2", "--timeout", "120", "--access-logfile", "-", "--error-logfile", "-"]
