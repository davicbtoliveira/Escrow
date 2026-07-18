# Generated manually for the initial scoped API-key lifecycle.

from __future__ import annotations

import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("organizations", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="ApiKey",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("name", models.CharField(max_length=100)),
                ("prefix", models.CharField(max_length=16, unique=True)),
                ("secret_hash", models.CharField(max_length=64)),
                ("scopes", models.JSONField(default=list)),
                ("expires_at", models.DateTimeField(blank=True, null=True)),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                ("last_used_at", models.DateTimeField(blank=True, null=True)),
                ("last_used_ip", models.GenericIPAddressField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="api_keys",
                        to="organizations.organization",
                    ),
                ),
            ],
            options={"ordering": ["-created_at", "id"]},
        ),
    ]
