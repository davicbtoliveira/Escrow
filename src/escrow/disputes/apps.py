from __future__ import annotations

from django.apps import AppConfig


class DisputesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "escrow.disputes"
    label = "disputes"
