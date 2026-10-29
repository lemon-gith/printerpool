import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name, default=False):
    return os.environ.get(name, str(int(default))).lower() in ("1", "true", "yes", "on")


def env_list(name, default=""):
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


def env_emails(name):
    from django.core.exceptions import ImproperlyConfigured
    from django.core.validators import EmailValidator

    addresses = env_list(name)
    for address in addresses:
        try:
            EmailValidator()(address)
        except Exception:
            raise ImproperlyConfigured(f"{name} contains an invalid email address: {address!r}")
    return addresses


SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]
DEBUG = env_bool("DJANGO_DEBUG")
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "axes",
    "accounts",
    "prints",
    "notifications",
    "ops",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "accounts.middleware.ForcePasswordChangeMiddleware",
    # Must be last: locks out a username/IP pair after repeated failed logins.
    "axes.middleware.AxesMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "config.context_processors.contact",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "printpool"),
        "USER": os.environ.get("POSTGRES_USER", "printpool"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
        "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        "CONN_MAX_AGE": 60,
        "CONN_HEALTH_CHECKS": True,
    }
}

AUTH_USER_MODEL = "accounts.User"

AUTHENTICATION_BACKENDS = [
    "axes.backends.AxesStandaloneBackend",
    "django.contrib.auth.backends.ModelBackend",
]

PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Flash messages live in the server-side session rather than a cookie.
MESSAGE_STORAGE = "django.contrib.messages.storage.session.SessionStorage"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

# Sessions: server-side (in Postgres), so logging someone out or deleting a user takes effect immediately.
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = 60 * 60 * 24 * 14

SECURE = env_bool("DJANGO_SECURE")
SESSION_COOKIE_SECURE = SECURE
CSRF_COOKIE_SECURE = SECURE
if SECURE:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    # Cloudflare already redirects to HTTPS; this is a second line of defence. The health check is exempt
    # because Docker calls it over plain HTTP from inside the container.
    SECURE_SSL_REDIRECT = True
    SECURE_REDIRECT_EXEMPT = [r"^health/$"]
    # HSTS makes browsers refuse plain HTTP for this host. Start short (1 hour); once HTTPS has been
    # working for a while, raise DJANGO_HSTS_SECONDS to a year (31536000). Long values are hard to undo.
    SECURE_HSTS_SECONDS = int(os.environ.get("DJANGO_HSTS_SECONDS", "3600"))

# Deliberately off: HSTS includeSubDomains/preload would force HTTPS on every sibling site under the domain
# (e.g. other uni services), which isn't ours to decide.
SILENCED_SYSTEM_CHECKS = ["security.W005", "security.W021"]

# Login throttling (django-axes)
AXES_FAILURE_LIMIT = 5
AXES_COOLOFF_TIME = 0.25  # hours
AXES_LOCKOUT_PARAMETERS = [["username", "ip_address"]]
AXES_RESET_ON_SUCCESS = True
AXES_LOCKOUT_TEMPLATE = "registration/lockout.html"
AXES_CLIENT_IP_CALLABLE = "accounts.ip.client_ip"
# Number of reverse proxies we run in front of the app (1: the gateway, used for plain-http local testing).
# Only set this when the app port is NOT reachable from the internet, or clients could forge their IP.
TRUSTED_PROXY_COUNT = int(os.environ.get("DJANGO_TRUSTED_PROXY_COUNT", "0"))
# Alternatively, a header that a trusted front end sets to the visitor's IP, e.g. CF-Connecting-IP behind a
# Cloudflare Tunnel. Takes priority over TRUSTED_PROXY_COUNT. Same rule: only when the app port isn't public.
CLIENT_IP_HEADER = os.environ.get("DJANGO_CLIENT_IP_HEADER", "").strip()

LANGUAGE_CODE = "en-gb"
TIME_ZONE = os.environ.get("TIME_ZONE", "Europe/London")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    # Uploaded models. Swap for an S3/R2 backend (django-storages) later without touching app code.
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
        "OPTIONS": {"location": os.environ.get("MEDIA_ROOT", "/data/media")},
    },
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}


MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "50"))
# Total size of the new files in one submission. Cloudflare's free plan rejects request bodies over 100 MB
# before they reach us, so checking it here (and in the browser) gives people a clear message instead.
MAX_REQUEST_UPLOAD_MB = int(os.environ.get("MAX_REQUEST_UPLOAD_MB", "100"))
ALLOWED_MODEL_EXTENSIONS = ["stl", "3mf"]
MAX_FILES_PER_REQUEST = 10
# Uploads above this are streamed to a temp file instead of held in memory.
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024

PRINT_CAP_GRAMS_PER_MONTH = int(os.environ.get("PRINT_CAP_GRAMS_PER_MONTH", "500"))

# Public address of the site, used for links in emails, e.g. https://print.example.ac.uk
SITE_URL = os.environ.get("DJANGO_SITE_URL", "http://localhost:8000")

# Outgoing email (verification codes and opt-in notifications). EMAIL_ENABLED=0 hides every email feature (the
# deployment default unless [email] is enabled in printpool.toml). Enabled without EMAIL_HOST, emails are printed to the
# app's log instead (`docker compose logs app`), which is handy for local testing.
EMAIL_ENABLED = env_bool("EMAIL_ENABLED", True)
EMAIL_HOST = os.environ.get("EMAIL_HOST", "")
if EMAIL_ENABLED and EMAIL_HOST:
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
    EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)  # STARTTLS on 587
    EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
    EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_PASSWORD") or os.environ.get("EMAIL_HOST_PASSWORD", "")
else:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
EMAIL_TIMEOUT = 10
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "PrintPool <printpool@localhost>")

# Optional contact addresses (comma-separated lists), shown as mailto: links addressed to all of them.
# Everyone sees the staff link; staff and admins also see the admin link.
CONTACT_STAFF_EMAILS = env_emails("CONTACT_STAFF_EMAILS")
CONTACT_ADMIN_EMAILS = env_emails("CONTACT_ADMIN_EMAILS")

# Backups (python manage.py backup; scheduled by the worker when BACKUP_ENABLED). Set from [backups] in printpool.toml.
BACKUP_ENABLED = env_bool("BACKUP_ENABLED", False)
BACKUP_FREQUENCY = os.environ.get("BACKUP_FREQUENCY", "daily")  # hourly | daily | weekly
BACKUP_TIME = os.environ.get("BACKUP_TIME", "03:30")
BACKUP_WEEKDAY = os.environ.get("BACKUP_WEEKDAY", "sunday")
BACKUP_KEEP = int(os.environ.get("BACKUP_KEEP", "14"))  # 0 keeps all
BACKUP_ENCRYPT = env_bool("BACKUP_ENCRYPT", True)
BACKUP_INCLUDE_FILES = env_bool("BACKUP_INCLUDE_FILES", False)
BACKUP_DIR = os.environ.get("BACKUP_DIR", "/backups")
BACKUP_PASSWORD = os.environ.get("BACKUP_PASSWORD", "")

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Optional error tracking: set SENTRY_DSN to send unhandled exceptions to Sentry (or a compatible service).
if os.environ.get("SENTRY_DSN"):
    import sentry_sdk

    sentry_sdk.init(
        dsn=os.environ["SENTRY_DSN"],
        environment=os.environ.get("SENTRY_ENVIRONMENT", "production"),
        send_default_pii=False,  # no usernames, IPs or request bodies; uploads and passwords stay out of it
        traces_sample_rate=0,
    )

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}

# Test-only adjustments; kept last so they override everything above.
if sys.argv[1:2] == ["test"]:
    # Tests render pages without running collectstatic first, so there is no manifest to look names up in.
    STORAGES["staticfiles"] = {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}
    # The test client speaks plain HTTP; don't redirect it even when run with a production .env.
    SECURE_SSL_REDIRECT = False
