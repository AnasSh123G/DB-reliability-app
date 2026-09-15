"""Django settings for a server that owns no database.

DATABASES = {} is the whole trick. ConnectionHandler.configure_settings then
substitutes django.db.backends.dummy for the default alias, so django.db never
opens a connection to anything. There is consequently no mechanism by which
django_migrations, auth_user, django_session or django_content_type could appear
in dbrel.sqlite — that is an impossibility rather than a policy, which is a much
stronger guarantee than a database router would give.

All data access goes through the SQLAlchemy layer in dbrel.db, which also means
the WAL and busy_timeout pragmas from db._set_sqlite_pragmas apply to every
connection the web makes. Django's own SQLite backend would apply none of them.
"""

from __future__ import annotations

import os

# Importing config runs the .env loader exactly once, in the one place that owns it.
from dbrel.config import TZ  # noqa: F401

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

SECRET_KEY = os.environ.get("DBREL_SECRET_KEY", "dev-only-not-a-secret")
DEBUG = os.environ.get("DBREL_WEB_DEBUG") == "1"
ALLOWED_HOSTS = [
    "127.0.0.1",
    "localhost",
    "[::1]",
    *[h for h in os.environ.get("DBREL_ALLOWED_HOSTS", "").split(",") if h],
]

# No auth, no sessions, no contenttypes, no admin, no messages. Every one of those
# wants tables; none of them is wanted here.
INSTALLED_APPS = [
    "django.contrib.staticfiles",
    "dbrel.web",
]

# CsrfViewMiddleware is absent because both pages are GET-only. Adding any POST
# means adding it back.
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "dbrel.web.urls"
WSGI_APPLICATION = "dbrel.web.wsgi.application"

DATABASES: dict = {}

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
            ]
        },
    }
]

LANGUAGE_CODE = "en-gb"
TIME_ZONE = "Europe/Berlin"
USE_I18N = False
USE_TZ = True

STATIC_URL = "/static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
