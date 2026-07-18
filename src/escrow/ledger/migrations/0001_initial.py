"""Create the append-only double-entry ledger and PostgreSQL integrity guards."""

from __future__ import annotations

import uuid

import django.db.models.deletion
from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor


def seed_system_chart(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    del schema_editor
    chart = apps.get_model("ledger", "ChartOfAccount")
    accounts = (
        ("PIX_CLEARING", "PIX clearing", "ASSET", "DEBIT"),
        ("FUNDS_PENDING_RISK", "Funds pending risk", "LIABILITY", "CREDIT"),
        ("ESCROW_LIABILITY", "Escrow liability", "LIABILITY", "CREDIT"),
        ("ORGANIZATION_PAYABLE", "Organization payable", "LIABILITY", "CREDIT"),
        ("PLATFORM_FEE_REVENUE", "Platform fee revenue", "REVENUE", "CREDIT"),
    )
    for code, name, account_type, normal_side in accounts:
        chart.objects.get_or_create(
            code=code,
            defaults={
                "name": name,
                "account_type": account_type,
                "normal_side": normal_side,
            },
        )


def create_postgres_guards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """Keep authoritative database protections where PostgreSQL can express them."""
    del apps
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            """
            CREATE OR REPLACE FUNCTION ledger_reject_mutation()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'ledger history is append-only';
            END;
            $$ LANGUAGE plpgsql;
            """
        )
        cursor.execute(
            """
            CREATE OR REPLACE FUNCTION ledger_validate_entry_currency()
            RETURNS trigger AS $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1
                    FROM ledger_ledgertransaction
                    WHERE id = NEW.ledger_transaction_id
                      AND currency = NEW.currency
                ) THEN
                    RAISE EXCEPTION 'ledger entry currency must match its transaction';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
        cursor.execute(
            """
            CREATE OR REPLACE FUNCTION ledger_validate_entries_balance()
            RETURNS trigger AS $$
            DECLARE
                target_transaction_id uuid;
            BEGIN
                target_transaction_id := COALESCE(
                    NEW.ledger_transaction_id,
                    OLD.ledger_transaction_id
                );
                IF EXISTS (
                    SELECT 1
                    FROM ledger_ledgerentry
                    WHERE ledger_transaction_id = target_transaction_id
                    GROUP BY currency
                    HAVING SUM(debit_minor) <> SUM(credit_minor)
                ) THEN
                    RAISE EXCEPTION 'ledger transaction is not balanced by currency';
                END IF;
                RETURN NULL;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
        cursor.execute(
            """
            CREATE OR REPLACE FUNCTION ledger_validate_transaction_balance()
            RETURNS trigger AS $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1
                    FROM ledger_ledgerentry
                    WHERE ledger_transaction_id = NEW.id
                ) OR EXISTS (
                    SELECT 1
                    FROM ledger_ledgerentry
                    WHERE ledger_transaction_id = NEW.id
                    GROUP BY currency
                    HAVING SUM(debit_minor) <> SUM(credit_minor)
                ) THEN
                    RAISE EXCEPTION 'ledger transaction must contain balanced entries';
                END IF;
                RETURN NULL;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
        cursor.execute(
            """
            CREATE TRIGGER ledger_transaction_append_only
            BEFORE UPDATE OR DELETE ON ledger_ledgertransaction
            FOR EACH ROW EXECUTE FUNCTION ledger_reject_mutation();
            """
        )
        cursor.execute(
            """
            CREATE TRIGGER ledger_entry_append_only
            BEFORE UPDATE OR DELETE ON ledger_ledgerentry
            FOR EACH ROW EXECUTE FUNCTION ledger_reject_mutation();
            """
        )
        cursor.execute(
            """
            CREATE TRIGGER ledger_entry_currency_matches_transaction
            BEFORE INSERT OR UPDATE ON ledger_ledgerentry
            FOR EACH ROW EXECUTE FUNCTION ledger_validate_entry_currency();
            """
        )
        cursor.execute(
            """
            CREATE CONSTRAINT TRIGGER ledger_entries_balance_deferred
            AFTER INSERT OR UPDATE OR DELETE ON ledger_ledgerentry
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION ledger_validate_entries_balance();
            """
        )
        cursor.execute(
            """
            CREATE CONSTRAINT TRIGGER ledger_transaction_balance_deferred
            AFTER INSERT ON ledger_ledgertransaction
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION ledger_validate_transaction_balance();
            """
        )


def drop_postgres_guards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    del apps
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            "DROP TRIGGER IF EXISTS ledger_transaction_balance_deferred ON ledger_ledgertransaction"
        )
        cursor.execute(
            "DROP TRIGGER IF EXISTS ledger_entries_balance_deferred ON ledger_ledgerentry"
        )
        cursor.execute(
            "DROP TRIGGER IF EXISTS ledger_entry_currency_matches_transaction ON ledger_ledgerentry"
        )
        cursor.execute("DROP TRIGGER IF EXISTS ledger_entry_append_only ON ledger_ledgerentry")
        cursor.execute(
            "DROP TRIGGER IF EXISTS ledger_transaction_append_only ON ledger_ledgertransaction"
        )
        cursor.execute("DROP FUNCTION IF EXISTS ledger_validate_transaction_balance()")
        cursor.execute("DROP FUNCTION IF EXISTS ledger_validate_entries_balance()")
        cursor.execute("DROP FUNCTION IF EXISTS ledger_validate_entry_currency()")
        cursor.execute("DROP FUNCTION IF EXISTS ledger_reject_mutation()")


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("payments", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="ChartOfAccount",
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
                (
                    "code",
                    models.CharField(
                        choices=[
                            ("PIX_CLEARING", "PIX clearing"),
                            ("FUNDS_PENDING_RISK", "Funds pending risk"),
                            ("ESCROW_LIABILITY", "Escrow liability"),
                            ("ORGANIZATION_PAYABLE", "Organization payable"),
                            ("PLATFORM_FEE_REVENUE", "Platform fee revenue"),
                        ],
                        max_length=64,
                        unique=True,
                    ),
                ),
                ("name", models.CharField(max_length=160)),
                (
                    "account_type",
                    models.CharField(
                        choices=[
                            ("ASSET", "Asset"),
                            ("LIABILITY", "Liability"),
                            ("REVENUE", "Revenue"),
                        ],
                        max_length=16,
                    ),
                ),
                (
                    "normal_side",
                    models.CharField(
                        choices=[("DEBIT", "Debit"), ("CREDIT", "Credit")],
                        max_length=6,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"ordering": ["code"]},
        ),
        migrations.CreateModel(
            name="LedgerTransaction",
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
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("FUNDING_RECEIVED", "Funding received"),
                            ("FUNDS_HELD", "Funds held"),
                            ("FUNDING_REJECTED", "Funding rejected"),
                            ("FUNDS_RELEASED", "Funds released"),
                            ("FUNDS_REFUNDED", "Funds refunded"),
                            ("REVERSAL", "Reversal"),
                        ],
                        max_length=32,
                    ),
                ),
                (
                    "currency",
                    models.CharField(
                        choices=[("BRL", "Brazilian real"), ("USD", "United States dollar")],
                        max_length=3,
                    ),
                ),
                ("idempotency_key", models.CharField(max_length=255, unique=True)),
                ("posting_hash", models.CharField(max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "transfer",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="ledger_transactions",
                        to="payments.transfer",
                    ),
                ),
            ],
            options={"ordering": ["created_at", "id"]},
        ),
        migrations.CreateModel(
            name="LedgerEntry",
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
                (
                    "currency",
                    models.CharField(
                        choices=[("BRL", "Brazilian real"), ("USD", "United States dollar")],
                        max_length=3,
                    ),
                ),
                ("debit_minor", models.PositiveBigIntegerField(default=0)),
                ("credit_minor", models.PositiveBigIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "account",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="ledger_entries",
                        to="ledger.chartofaccount",
                    ),
                ),
                (
                    "ledger_transaction",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="entries",
                        to="ledger.ledgertransaction",
                    ),
                ),
            ],
            options={"ordering": ["created_at", "id"]},
        ),
        migrations.AddConstraint(
            model_name="ledgertransaction",
            constraint=models.CheckConstraint(
                condition=models.Q(currency__in=["BRL", "USD"]),
                name="ledger_transaction_currency_is_brl_or_usd",
            ),
        ),
        migrations.AddConstraint(
            model_name="ledgertransaction",
            constraint=models.CheckConstraint(
                condition=~models.Q(idempotency_key=""),
                name="ledger_transaction_idempotency_key_not_empty",
            ),
        ),
        migrations.AddConstraint(
            model_name="ledgertransaction",
            constraint=models.UniqueConstraint(
                fields=("transfer", "kind"),
                name="ledger_transaction_transfer_kind_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="ledgerentry",
            constraint=models.CheckConstraint(
                condition=(models.Q(debit_minor__gt=0) & models.Q(credit_minor=0))
                | (models.Q(debit_minor=0) & models.Q(credit_minor__gt=0)),
                name="ledger_entry_exactly_one_side_positive",
            ),
        ),
        migrations.AddConstraint(
            model_name="ledgerentry",
            constraint=models.CheckConstraint(
                condition=models.Q(currency__in=["BRL", "USD"]),
                name="ledger_entry_currency_is_brl_or_usd",
            ),
        ),
        migrations.AddIndex(
            model_name="ledgerentry",
            index=models.Index(
                fields=["ledger_transaction", "currency"],
                name="ledger_ledg_ledger__ddd13d_idx",
            ),
        ),
        migrations.RunPython(seed_system_chart, migrations.RunPython.noop),
        migrations.RunPython(create_postgres_guards, drop_postgres_guards),
    ]
