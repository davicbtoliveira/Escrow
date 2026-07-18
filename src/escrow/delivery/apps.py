from __future__ import annotations

from django.apps import AppConfig


class DeliveryConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "escrow.delivery"
    label = "delivery"
