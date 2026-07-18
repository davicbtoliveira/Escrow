"""ASGI configuration for Escrow."""

from __future__ import annotations

import os

from channels.routing import ProtocolTypeRouter, URLRouter  # type: ignore[import-untyped]
from channels.security.websocket import AllowedHostsOriginValidator  # type: ignore[import-untyped]
from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "escrow.settings")

django_asgi_application = get_asgi_application()

from escrow.notifications.routing import websocket_urlpatterns  # noqa: E402

application = ProtocolTypeRouter(
    {
        "http": django_asgi_application,
        "websocket": AllowedHostsOriginValidator(URLRouter(websocket_urlpatterns)),
    }
)
