"""WebSocket routes owned by the notification module."""

from django.urls import re_path

from escrow.notifications.consumers import PublicCheckoutStatusConsumer

websocket_urlpatterns = [
    re_path(
        r"^ws/checkout/(?P<checkout_token>chk_[A-Za-z0-9_-]{1,251})/$",
        PublicCheckoutStatusConsumer.as_asgi(),
    ),
]
