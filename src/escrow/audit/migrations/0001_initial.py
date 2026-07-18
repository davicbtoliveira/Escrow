"""Initial immutable audit history schema."""

from __future__ import annotations

import uuid

import django.db.models.deletion
from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor


def install_postgres_immutability(_: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(
        """
        CREATE FUNCTION escrow_prevent_audit_event_mutation() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'audit history is immutable';
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER audit_event_immutable
        BEFORE UPDATE OR DELETE ON audit_auditevent
        FOR EACH ROW EXECUTE FUNCTION escrow_prevent_audit_event_mutation();
        """
    )


def remove_postgres_immutability(_: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(
        """
        DROP TRIGGER IF EXISTS audit_event_immutable ON audit_auditevent;
        DROP FUNCTION IF EXISTS escrow_prevent_audit_event_mutation();
        """
    )


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("agreements", "0002_protect_idempotency_checkout_token"),
        ("identity", "0001_initial"),
        ("organizations", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="AuditEvent",
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
                ("event_type", models.CharField(max_length=100)),
                ("correlation_id", models.CharField(blank=True, max_length=128)),
                ("payload", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "actor",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="audit_events",
                        to="identity.user",
                    ),
                ),
                (
                    "agreement",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="audit_events",
                        to="agreements.escrowagreement",
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="audit_events",
                        to="organizations.organization",
                    ),
                ),
            ],
            options={"ordering": ["created_at", "id"]},
        ),
        migrations.AddIndex(
            model_name="auditevent",
            index=models.Index(
                fields=["organization", "created_at"],
                name="audit_audi_organiz_7d7bda_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="auditevent",
            index=models.Index(
                fields=["agreement", "created_at"],
                name="audit_audi_agreeme_94978d_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="auditevent",
            index=models.Index(
                fields=["event_type", "created_at"],
                name="audit_audi_event_t_3ad60e_idx",
            ),
        ),
        migrations.RunPython(install_postgres_immutability, remove_postgres_immutability),
    ]
