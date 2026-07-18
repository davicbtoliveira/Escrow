"""WSGI configuration for management and compatibility tooling."""

from __future__ import annotations

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "escrow.settings")

application = get_wsgi_application()
