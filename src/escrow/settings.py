"""Django settings for the local, fictional escrow simulation."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import unquote, urlparse

BASE_DIR = Path(__file__).resolve().parent.parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "unsafe-development-key")
DEBUG = os.environ.get("DJANGO_DEBUG", "true").lower() == "true"
ALLOWED_HOSTS = [
    host
    for host in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if host
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "drf_spectacular",
    "escrow.identity.apps.IdentityConfig",
    "escrow.integrations.apps.IntegrationsConfig",
    "escrow.organizations.apps.OrganizationsConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "escrow.correlation.CorrelationIdMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "escrow.urls"
AUTH_USER_MODEL = "identity.User"
AUTHENTICATION_BACKENDS = ["django.contrib.auth.backends.ModelBackend"]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "escrow.wsgi.application"
ASGI_APPLICATION = "escrow.asgi.application"


def database_config(database_url: str) -> dict[str, object]:
    """Translate the narrow local DATABASE_URL contract into Django settings."""
    parsed = urlparse(database_url)
    if parsed.scheme == "sqlite":
        database_name = unquote(parsed.path) if parsed.path else ":memory:"
        if database_name == "/:memory:":
            database_name = ":memory:"
        return {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": database_name,
        }
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ValueError("DATABASE_URL must use sqlite, postgres, or postgresql")
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": parsed.path.removeprefix("/"),
        "USER": unquote(parsed.username or ""),
        "PASSWORD": unquote(parsed.password or ""),
        "HOST": parsed.hostname or "localhost",
        "PORT": str(parsed.port or 5432),
        "CONN_MAX_AGE": 0,
    }


DATABASES = {
    "default": database_config(os.environ.get("DATABASE_URL", "sqlite:///:memory:")),
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "pt-br"
TIME_ZONE = "America/Campo_Grande"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
RABBITMQ_URL = os.environ.get("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/%2F")
HEALTHCHECK_TIMEOUT_SECONDS = float(os.environ.get("HEALTHCHECK_TIMEOUT_SECONDS", "1"))

SESSION_COOKIE_NAME = "escrow_session"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = (
    os.environ.get("DJANGO_SESSION_COOKIE_SECURE", str(not DEBUG)).lower() == "true"
)
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_NAME = "escrow_csrf"
CSRF_COOKIE_SECURE = os.environ.get("DJANGO_CSRF_COOKIE_SECURE", str(not DEBUG)).lower() == "true"
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_FAILURE_VIEW = "escrow.http.csrf_failure"

FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:5173")
HIBP_MODE = os.environ.get("HIBP_MODE", "mock" if DEBUG else "live")
HIBP_MOCK_PWNED_PASSWORDS = os.environ.get("HIBP_MOCK_PWNED_PASSWORDS", "")
HIBP_TIMEOUT_SECONDS = float(os.environ.get("HIBP_TIMEOUT_SECONDS", "3"))

EMAIL_DELIVERY_BACKEND = os.environ.get(
    "EMAIL_DELIVERY_BACKEND", "django" if DEBUG else "ministack"
)
MINISTACK_ENDPOINT_URL = os.environ.get("MINISTACK_ENDPOINT_URL", "http://localhost:4566")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID", "000000000000")
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY", "local-development-only")
SES_FROM_EMAIL = os.environ.get("SES_FROM_EMAIL", "noreply@escrow.local")

API_KEY_HMAC_SECRET = os.environ.get("API_KEY_HMAC_SECRET", SECRET_KEY)
API_KEY_RATE_LIMIT_MAX = max(1, int(os.environ.get("API_KEY_RATE_LIMIT_MAX", "100")))
API_KEY_RATE_LIMIT_WINDOW_SECONDS = max(
    1, int(os.environ.get("API_KEY_RATE_LIMIT_WINDOW_SECONDS", "60"))
)
API_KEY_RATE_LIMIT_BURST = max(0, int(os.environ.get("API_KEY_RATE_LIMIT_BURST", "20")))
API_KEY_ROTATION_OVERLAP_SECONDS = max(
    0, int(os.environ.get("API_KEY_ROTATION_OVERLAP_SECONDS", "3600"))
)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"json": {"()": "escrow.logging.JsonFormatter"}},
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "json",
            "stream": "ext://sys.stdout",
        }
    },
    "loggers": {
        "escrow": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}

REST_FRAMEWORK = {
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "EXCEPTION_HANDLER": "escrow.http.drf_exception_handler",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Escrow Integration API",
    "VERSION": "v1",
    "SERVE_INCLUDE_SCHEMA": False,
    "APPEND_COMPONENTS": {
        "securitySchemes": {
            "ApiKeyAuth": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "escrow API key",
            }
        }
    },
}
