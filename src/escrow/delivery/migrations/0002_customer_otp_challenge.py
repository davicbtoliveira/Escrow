# ruff: noqa: E501
from __future__ import annotations

import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("delivery", "0001_initial")]

    operations = [
        migrations.CreateModel(
            name="CustomerOtpChallenge",
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
                ("code_hash", models.CharField(max_length=64)),
                ("sent_at", models.DateTimeField()),
                ("expires_at", models.DateTimeField()),
                ("verification_attempts", models.PositiveSmallIntegerField(default=0)),
                ("verified_at", models.DateTimeField(blank=True, null=True)),
                ("authorization_token_hash", models.CharField(blank=True, max_length=64, null=True)),
                ("authorization_expires_at", models.DateTimeField(blank=True, null=True)),
                ("consumed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "agreement",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="customer_otp_challenges",
                        to="agreements.escrowagreement",
                    ),
                ),
            ],
            options={"ordering": ["-created_at", "id"]},
        ),
        migrations.AddIndex(
            model_name="customerotpchallenge",
            index=models.Index(fields=["agreement", "created_at"], name="delivery_cu_agreeme_280d24_idx"),
        ),
    ]
