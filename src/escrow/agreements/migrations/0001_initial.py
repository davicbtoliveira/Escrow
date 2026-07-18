# Generated manually from the initial agreement and idempotency schema.

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
            name="EscrowAgreement",
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
                ("external_customer_id", models.CharField(max_length=128)),
                ("customer_name_masked", models.CharField(max_length=200)),
                ("customer_email_masked", models.CharField(max_length=254)),
                ("customer_document_masked", models.CharField(max_length=32)),
                ("customer_document_kind", models.CharField(max_length=4)),
                ("customer_email_blind_index", models.CharField(db_index=True, max_length=64)),
                ("customer_document_blind_index", models.CharField(db_index=True, max_length=64)),
                ("customer_pii_ciphertext", models.BinaryField()),
                ("customer_pii_nonce", models.BinaryField()),
                ("customer_pii_encrypted_data_key", models.BinaryField()),
                ("customer_pii_kms_key_id", models.CharField(max_length=512)),
                ("checkout_token_hash", models.CharField(max_length=64, unique=True)),
                ("amount_minor", models.PositiveBigIntegerField()),
                (
                    "currency",
                    models.CharField(
                        choices=[("BRL", "Brazilian real"), ("USD", "United States dollar")],
                        max_length=3,
                    ),
                ),
                ("fee_bps", models.PositiveIntegerField()),
                ("delivery_window_days", models.PositiveSmallIntegerField()),
                ("delivery_due_at", models.DateTimeField(blank=True, null=True)),
                ("inspection_deadline_at", models.DateTimeField(blank=True, null=True)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("AWAITING_PAYMENT", "Awaiting payment"),
                            ("FUNDING_PROCESSING", "Funding processing"),
                            ("HELD", "Held in escrow"),
                            ("REVIEW_REQUIRED", "Risk review required"),
                            ("FUNDING_REJECTED", "Funding rejected"),
                            ("INSPECTION", "Inspection"),
                            ("DISPUTED", "Disputed"),
                            ("RELEASE_PENDING", "Release pending"),
                            ("RELEASED", "Released"),
                            ("REFUND_PENDING", "Refund pending"),
                            ("REFUNDED", "Refunded"),
                            ("CANCELLED", "Cancelled"),
                        ],
                        default="AWAITING_PAYMENT",
                        max_length=32,
                    ),
                ),
                ("version", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="agreements",
                        to="organizations.organization",
                    ),
                ),
            ],
            options={"ordering": ["-created_at", "id"]},
        ),
        migrations.CreateModel(
            name="IdempotencyRecord",
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
                ("method", models.CharField(max_length=8)),
                ("route", models.CharField(max_length=255)),
                ("idempotency_key", models.CharField(max_length=255)),
                ("request_hash", models.CharField(max_length=64)),
                ("response_status", models.PositiveSmallIntegerField()),
                ("response_body", models.JSONField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "agreement",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="idempotency_records",
                        to="agreements.escrowagreement",
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="idempotency_records",
                        to="organizations.organization",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="escrowagreement",
            constraint=models.CheckConstraint(
                condition=models.Q(("amount_minor__gt", 0)),
                name="agreements_amount_minor_positive",
            ),
        ),
        migrations.AddConstraint(
            model_name="escrowagreement",
            constraint=models.CheckConstraint(
                condition=models.Q(("currency__in", ["BRL", "USD"])),
                name="agreements_currency_is_brl_or_usd",
            ),
        ),
        migrations.AddConstraint(
            model_name="escrowagreement",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("delivery_window_days__gte", 1),
                    ("delivery_window_days__lte", 90),
                ),
                name="agreements_delivery_window_between_1_and_90",
            ),
        ),
        migrations.AddConstraint(
            model_name="escrowagreement",
            constraint=models.CheckConstraint(
                condition=models.Q(("fee_bps__gte", 0), ("fee_bps__lte", 10_000)),
                name="agreements_fee_bps_between_0_and_10000",
            ),
        ),
        migrations.AddIndex(
            model_name="idempotencyrecord",
            index=models.Index(
                fields=["organization", "created_at"],
                name="agreements__organiz_d9fbb3_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="idempotencyrecord",
            constraint=models.UniqueConstraint(
                fields=("organization", "method", "route", "idempotency_key"),
                name="agreements_idempotency_scope_unique",
            ),
        ),
    ]
