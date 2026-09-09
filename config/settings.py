import os
from datetime import timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

def secret(name, default=''):
    path = os.environ.get(name + '_FILE')
    return Path(path).read_text().strip() if path else os.environ.get(name, default)

DEBUG = os.environ.get('DEBUG', 'false').lower() == 'true'
SECRET_KEY = secret('SECRET_KEY', 'development-only-change-me' if DEBUG else '')
if not SECRET_KEY:
    raise RuntimeError('SECRET_KEY must be configured')
ALLOWED_HOSTS = os.environ.get('ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',')
CSRF_TRUSTED_ORIGINS = [x for x in os.environ.get('CSRF_TRUSTED_ORIGINS', '').split(',') if x]
INSTALLED_APPS = ['django.contrib.admin', 'django.contrib.auth', 'django.contrib.contenttypes',
                 'django.contrib.sessions', 'django.contrib.messages', 'django.contrib.staticfiles', 'axes', 'billing']
MIDDLEWARE = ['django.middleware.security.SecurityMiddleware', 'whitenoise.middleware.WhiteNoiseMiddleware',
              'django.contrib.sessions.middleware.SessionMiddleware', 'django.middleware.common.CommonMiddleware',
              'django.middleware.csrf.CsrfViewMiddleware', 'django.contrib.auth.middleware.AuthenticationMiddleware',
              'django.contrib.messages.middleware.MessageMiddleware', 'django.middleware.clickjacking.XFrameOptionsMiddleware',
              'axes.middleware.AxesMiddleware']
ROOT_URLCONF = 'config.urls'
TEMPLATES = [{'BACKEND': 'django.template.backends.django.DjangoTemplates', 'DIRS': [BASE_DIR / 'templates'],
              'APP_DIRS': True, 'OPTIONS': {'context_processors': ['django.template.context_processors.request',
                  'django.contrib.auth.context_processors.auth', 'django.contrib.messages.context_processors.messages']}}]
WSGI_APPLICATION = 'config.wsgi.application'
if os.environ.get('DB_HOST'):
    DATABASES = {'default': {'ENGINE': 'django.db.backends.postgresql', 'NAME': os.environ.get('DB_NAME', 'billing'),
        'USER': os.environ.get('DB_USER', 'billing'), 'PASSWORD': secret('DB_PASSWORD'),
        'HOST': os.environ['DB_HOST'], 'PORT': os.environ.get('DB_PORT', '5432'), 'CONN_MAX_AGE': 60}}
elif DEBUG or os.environ.get('TESTING') == 'true':
    DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': BASE_DIR / 'dev.sqlite3'}}
else:
    raise RuntimeError('PostgreSQL DB_HOST is required in production')
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator', 'OPTIONS': {'min_length': 12}},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'}]
AUTHENTICATION_BACKENDS = ['axes.backends.AxesStandaloneBackend', 'django.contrib.auth.backends.ModelBackend']
AXES_FAILURE_LIMIT = 8
AXES_COOLOFF_TIME = timedelta(minutes=20)
AXES_LOCKOUT_PARAMETERS = ['username', 'ip_address']
AXES_IPWARE_PROXY_COUNT = 1
AXES_IPWARE_META_PRECEDENCE_ORDER = ['HTTP_X_FORWARDED_FOR', 'REMOTE_ADDR']
AXES_RESET_ON_SUCCESS = True
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'static']
STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
            'staticfiles': {'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage'}}
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
LOGIN_URL = '/login/'
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/login/'
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_AGE = 28800
SESSION_COOKIE_SAMESITE = 'Lax'
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SECURE_SSL_REDIRECT = not DEBUG
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_HSTS_SECONDS = 31536000 if not DEBUG else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = False
# Preload is intentionally disabled for a temporary deployment hostname.
SILENCED_SYSTEM_CHECKS = ['security.W021']
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = 'DENY'
COLLECTOR_ROLE_ARN = os.environ.get('COLLECTOR_ROLE_ARN', '')
ARTIFACT_BUCKET = os.environ.get('ARTIFACT_BUCKET', '')
AWS_REGION = os.environ.get('AWS_REGION', 'ap-south-1')
HISTORY_MONTHS = int(os.environ.get('HISTORY_MONTHS', '6'))
# Background worker / durable job queue (see billing/jobs.py and billing/scheduler.py)
WORKER_CONCURRENCY = int(os.environ.get('WORKER_CONCURRENCY', '3'))
WORKER_POLL_SECONDS = float(os.environ.get('WORKER_POLL_SECONDS', '5'))
WORKER_TICK_SECONDS = int(os.environ.get('WORKER_TICK_SECONDS', '30'))
JOB_LEASE_SECONDS = int(os.environ.get('JOB_LEASE_SECONDS', '600'))
JOB_MAX_ATTEMPTS = int(os.environ.get('JOB_MAX_ATTEMPTS', '6'))
JOB_BACKOFF_SECONDS = int(os.environ.get('JOB_BACKOFF_SECONDS', '60'))
JOB_BACKOFF_CAP_SECONDS = int(os.environ.get('JOB_BACKOFF_CAP_SECONDS', '3600'))
SCHEDULE_JITTER_SECONDS = int(os.environ.get('SCHEDULE_JITTER_SECONDS', '900'))
RECONCILE_MONTHS = int(os.environ.get('RECONCILE_MONTHS', '3'))
# AWS request limits: per-job request cap, pacing and throttle retries (Cost Explorer bills per request)
MAX_REQUESTS_PER_JOB = int(os.environ.get('MAX_REQUESTS_PER_JOB', '400'))
MAX_PAGES_PER_REQUEST = int(os.environ.get('MAX_PAGES_PER_REQUEST', '1000'))
AWS_REQUEST_INTERVAL_SECONDS = float(os.environ.get('AWS_REQUEST_INTERVAL_SECONDS', '0'))
AWS_THROTTLE_RETRIES = int(os.environ.get('AWS_THROTTLE_RETRIES', '3'))
AWS_THROTTLE_SLEEP_FACTOR = float(os.environ.get('AWS_THROTTLE_SLEEP_FACTOR', '1.0'))
BUDGET_AWS_FORECASTS = os.environ.get('BUDGET_AWS_FORECASTS', 'true').lower() == 'true'
RUN_RATE_MIN_DAYS = int(os.environ.get('RUN_RATE_MIN_DAYS', '3'))
LOGGING = {'version': 1, 'disable_existing_loggers': False,
           'handlers': {'console': {'class': 'logging.StreamHandler'}},
           'root': {'handlers': ['console'], 'level': 'INFO'}}

# Security is enabled by default. Tests may override settings explicitly.
RUNTIME_ROLE = os.environ.get('RUNTIME_ROLE', 'web')  # web | collector | admin
SUPPORTED_AWS_PARTITIONS = ('aws',)  # other partitions need a matching collector and service endpoint validation
COLLECTOR_ALLOWLIST_FILE = os.environ.get('COLLECTOR_ALLOWLIST_FILE', '')
REQUIRE_CONNECTION_APPROVAL = True
ENFORCE_CUSTOMER_AUTHORIZATION = True
MFA_REQUIRED = True
EXTERNAL_PORTAL_ENABLED = os.environ.get('EXTERNAL_PORTAL_ENABLED', 'false').lower() == 'true'
LIVE_NOTIFICATIONS_ENABLED = os.environ.get('LIVE_NOTIFICATIONS_ENABLED', 'false').lower() == 'true'

DATABASE_RLS_ENABLED = os.environ.get('DATABASE_RLS_ENABLED', 'false').lower() == 'true'
INSTALLED_APPS += ['django_otp', 'django_otp.plugins.otp_totp', 'mozilla_django_oidc']
MIDDLEWARE.insert(MIDDLEWARE.index('django.contrib.auth.middleware.AuthenticationMiddleware') + 1, 'django_otp.middleware.OTPMiddleware')
MIDDLEWARE.append('billing.middleware.SecurityMiddleware')
OTP_TOTP_ISSUER = 'Cloud Billing Console'
OTP_TOTP_THROTTLE_FACTOR = 1
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
LOGGING['filters'] = {'redact': {'()': 'billing.redaction.RedactionFilter'}}
LOGGING['handlers']['console']['filters'] = ['redact']
LOGGING['loggers'] = {'botocore': {'level': 'WARNING'}, 'urllib3': {'level': 'WARNING'}, 'mozilla_django_oidc': {'level': 'WARNING'}}
OIDC_ENABLED = os.environ.get('OIDC_ENABLED', 'false').lower() == 'true'
OIDC_RP_CLIENT_ID = os.environ.get('OIDC_RP_CLIENT_ID', '')
OIDC_RP_CLIENT_SECRET = secret('OIDC_RP_CLIENT_SECRET')
OIDC_ISSUER = os.environ.get('OIDC_ISSUER', '')
OIDC_OP_AUTHORIZATION_ENDPOINT = os.environ.get('OIDC_OP_AUTHORIZATION_ENDPOINT', '')
OIDC_OP_TOKEN_ENDPOINT = os.environ.get('OIDC_OP_TOKEN_ENDPOINT', '')
OIDC_OP_USER_ENDPOINT = os.environ.get('OIDC_OP_USER_ENDPOINT', '')
OIDC_OP_JWKS_ENDPOINT = os.environ.get('OIDC_OP_JWKS_ENDPOINT', '')
OIDC_RP_SIGN_ALGO = 'RS256'
OIDC_CREATE_USER = False
OIDC_USE_NONCE = True
OIDC_USE_PKCE = True
OIDC_VERIFY_SSL = True
OIDC_STORE_ACCESS_TOKEN = False
OIDC_STORE_ID_TOKEN = False
OIDC_TIMEOUT = 10
AUTHENTICATION_BACKENDS += ['billing.oidc.BillingOIDCBackend']

if os.environ.get('DB_HOST') and os.environ.get('DB_SSLMODE'):
    DATABASES['default']['OPTIONS'] = {'sslmode': os.environ['DB_SSLMODE']}
    if os.environ.get('DB_SSLROOTCERT'):
        DATABASES['default']['OPTIONS']['sslrootcert'] = os.environ['DB_SSLROOTCERT']
UNUSUAL_EXPORT_THRESHOLD = 20
NOTIFICATION_GATE_REFERENCE = os.environ.get('NOTIFICATION_GATE_REFERENCE', '')
EMAIL_BACKEND = os.environ.get('EMAIL_BACKEND', 'django.core.mail.backends.locmem.EmailBackend')
DEFAULT_FROM_EMAIL = os.environ.get('DEFAULT_FROM_EMAIL', 'billing@example.invalid')
EMAIL_HOST = os.environ.get('EMAIL_HOST', '')
EMAIL_PORT = int(os.environ.get('EMAIL_PORT', '587'))
EMAIL_USE_TLS = True
EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = secret('EMAIL_HOST_PASSWORD')
