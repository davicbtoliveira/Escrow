from __future__ import annotations

import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [("agreements", "0003_funding_lifecycle_fields")]

    operations = [
        migrations.CreateModel(
            name="DeliveryReport",
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
                ("idempotency_key", models.CharField(max_length=255)),
                ("reported_at", models.DateTimeField()),
                ("inspection_deadline_at", models.DateTimeField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "agreement",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="delivery_report",
                        to="agreements.escrowagreement",
                    ),
                ),
            ],
            options={"ordering": ["reported_at", "id"]},
        )
    ]
