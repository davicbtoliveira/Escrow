"""Move replayable checkout capabilities out of JSON response snapshots."""

from __future__ import annotations

from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor


def scrub_legacy_idempotency_secrets(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """Remove legacy raw-token URLs and unkeyed PII request verifiers."""
    del schema_editor
    idempotency_record = apps.get_model("agreements", "IdempotencyRecord")
    for record in idempotency_record.objects.iterator():
        response_body = record.response_body
        update_fields: list[str] = []
        if isinstance(response_body, dict) and "checkout_url" in response_body:
            sanitized = dict(response_body)
            sanitized.pop("checkout_url", None)
            record.response_body = sanitized
            update_fields.append("response_body")
        if record.request_hash:
            record.request_hash = ""
            update_fields.append("request_hash")
        if update_fields:
            record.save(update_fields=update_fields)


class Migration(migrations.Migration):
    dependencies = [
        ("agreements", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="idempotencyrecord",
            name="checkout_token_ciphertext",
            field=models.BinaryField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="idempotencyrecord",
            name="checkout_token_nonce",
            field=models.BinaryField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="idempotencyrecord",
            name="checkout_token_encrypted_data_key",
            field=models.BinaryField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="idempotencyrecord",
            name="checkout_token_kms_key_id",
            field=models.CharField(blank=True, max_length=512, null=True),
        ),
        migrations.RunPython(scrub_legacy_idempotency_secrets, migrations.RunPython.noop),
    ]
