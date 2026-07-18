from __future__ import annotations

import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("payments", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="FundingRiskPolicy",
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
                ("version", models.CharField(max_length=64, unique=True)),
                ("configuration", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"ordering": ["created_at", "id"]},
        ),
        migrations.CreateModel(
            name="FundingRiskDecision",
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
                ("policy_version", models.CharField(max_length=64)),
                ("inputs", models.JSONField()),
                ("score", models.PositiveSmallIntegerField()),
                ("reasons", models.JSONField(default=list)),
                ("outcome", models.CharField(max_length=32)),
                ("evaluated_at", models.DateTimeField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "policy",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="decisions",
                        to="risk.fundingriskpolicy",
                    ),
                ),
                (
                    "transfer",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="funding_risk_decision",
                        to="payments.transfer",
                    ),
                ),
            ],
            options={"ordering": ["evaluated_at", "id"]},
        ),
        migrations.AddConstraint(
            model_name="fundingriskdecision",
            constraint=models.CheckConstraint(
                condition=models.Q(("score__gte", 0), ("score__lte", 100)),
                name="risk_funding_score_between_0_and_100",
            ),
        ),
        migrations.AddConstraint(
            model_name="fundingriskdecision",
            constraint=models.CheckConstraint(
                condition=models.Q(("outcome__in", ["APPROVED", "REVIEW_REQUIRED", "REJECTED"])),
                name="risk_funding_outcome_is_known",
            ),
        ),
    ]
