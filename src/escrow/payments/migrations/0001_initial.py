# Generated manually for the initial payment-intent and sandbox callback schema.

from __future__ import annotations

import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("agreements", "0002_protect_idempotency_checkout_token"),
    ]

    operations = [
        migrations.CreateModel(
            name="SandboxPixCharge",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("amount_minor", models.PositiveBigIntegerField()),
                (
                    "currency",
                    models.CharField(
                        choices=[("BRL", "Brazilian real"), ("USD", "United States dollar")],
                        max_length=3,
                    ),
                ),
                ("provider_reference", models.CharField(max_length=128, unique=True)),
                ("idempotency_key", models.CharField(max_length=255)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("PENDING", "Pending"),
                            ("CONFIRMED", "Confirmed"),
                            ("REJECTED", "Rejected"),
                        ],
                        default="PENDING",
                        max_length=16,
                    ),
                ),
                ("confirmed_at", models.DateTimeField(blank=True, null=True)),
                ("rejected_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "agreement",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="sandbox_pix_charge",
                        to="agreements.escrowagreement",
                    ),
                ),
            ],
            options={"ordering": ["-created_at", "id"]},
        ),
        migrations.CreateModel(
            name="Transfer",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("FUNDING", "Funding"),
                            ("RELEASE", "Release"),
                            ("REFUND", "Refund"),
                        ],
                        max_length=16,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("PENDING", "Pending"),
                            ("PROCESSING", "Processing"),
                            ("COMPLETED", "Completed"),
                            ("FAILED", "Failed"),
                        ],
                        default="PENDING",
                        max_length=16,
                    ),
                ),
                ("amount_minor", models.PositiveBigIntegerField()),
                (
                    "currency",
                    models.CharField(
                        choices=[("BRL", "Brazilian real"), ("USD", "United States dollar")],
                        max_length=3,
                    ),
                ),
                (
                    "provider",
                    models.CharField(
                        choices=[
                            ("SANDBOX_PIX", "Sandbox PIX"),
                            ("INTERNAL", "Internal"),
                        ],
                        max_length=32,
                    ),
                ),
                ("provider_reference", models.CharField(max_length=128)),
                ("provider_event_id", models.CharField(blank=True, max_length=128, null=True)),
                ("idempotency_key", models.CharField(max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "agreement",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="transfers",
                        to="agreements.escrowagreement",
                    ),
                ),
            ],
            options={"ordering": ["created_at", "id"]},
        ),
        migrations.CreateModel(
            name="ProviderCallbackReceipt",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                (
                    "provider",
                    models.CharField(choices=[("SANDBOX_PIX", "Sandbox PIX")], max_length=32),
                ),
                ("provider_event_id", models.CharField(max_length=128)),
                (
                    "outcome",
                    models.CharField(
                        choices=[("CONFIRMED", "Confirmed"), ("REJECTED", "Rejected")],
                        max_length=16,
                    ),
                ),
                ("payload_hash", models.CharField(max_length=64)),
                ("signature_timestamp", models.PositiveBigIntegerField()),
                ("received_at", models.DateTimeField(auto_now_add=True)),
                (
                    "charge",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="callback_receipts",
                        to="payments.sandboxpixcharge",
                    ),
                ),
                (
                    "transfer",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="provider_callback_receipts",
                        to="payments.transfer",
                    ),
                ),
            ],
            options={"ordering": ["received_at", "id"]},
        ),
        migrations.AddConstraint(
            model_name="sandboxpixcharge",
            constraint=models.CheckConstraint(
                condition=models.Q(("amount_minor__gt", 0)),
                name="payments_charge_amount_minor_positive",
            ),
        ),
        migrations.AddConstraint(
            model_name="sandboxpixcharge",
            constraint=models.CheckConstraint(
                condition=models.Q(("currency__in", ["BRL", "USD"])),
                name="payments_charge_currency_is_brl_or_usd",
            ),
        ),
        migrations.AddConstraint(
            model_name="transfer",
            constraint=models.CheckConstraint(
                condition=models.Q(("amount_minor__gt", 0)),
                name="payments_transfer_amount_minor_positive",
            ),
        ),
        migrations.AddConstraint(
            model_name="transfer",
            constraint=models.CheckConstraint(
                condition=models.Q(("currency__in", ["BRL", "USD"])),
                name="payments_transfer_currency_is_brl_or_usd",
            ),
        ),
        migrations.AddConstraint(
            model_name="transfer",
            constraint=models.UniqueConstraint(
                fields=("agreement", "kind"), name="payments_transfer_one_kind_per_agreement"
            ),
        ),
        migrations.AddConstraint(
            model_name="transfer",
            constraint=models.UniqueConstraint(
                fields=("agreement", "idempotency_key"),
                name="payments_transfer_idempotency_per_agreement",
            ),
        ),
        migrations.AddConstraint(
            model_name="transfer",
            constraint=models.UniqueConstraint(
                fields=("provider", "provider_reference"),
                name="payments_transfer_provider_reference_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="transfer",
            constraint=models.UniqueConstraint(
                fields=("provider", "provider_event_id"),
                name="payments_transfer_provider_event_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="providercallbackreceipt",
            constraint=models.UniqueConstraint(
                fields=("provider", "provider_event_id"),
                name="payments_callback_provider_event_unique",
            ),
        ),
        migrations.AddIndex(
            model_name="transfer",
            index=models.Index(
                fields=["agreement", "status"], name="payments_tr_agreeme_68b233_idx"
            ),
        ),
    ]
