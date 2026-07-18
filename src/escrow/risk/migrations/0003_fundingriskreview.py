from __future__ import annotations

import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("risk", "0002_fundingriskdecision_policy_snapshot"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="FundingRiskReview",
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
                ("command_id", models.CharField(max_length=128, unique=True)),
                (
                    "outcome",
                    models.CharField(
                        choices=[("APPROVED", "Approved"), ("REJECTED", "Rejected")],
                        max_length=16,
                    ),
                ),
                ("rationale", models.TextField(max_length=1_000)),
                ("reviewed_at", models.DateTimeField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "analyst",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="funding_risk_reviews",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "decision",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="manual_review",
                        to="risk.fundingriskdecision",
                    ),
                ),
            ],
            options={
                "ordering": ["reviewed_at", "id"],
                "indexes": [
                    models.Index(
                        fields=["outcome", "reviewed_at"],
                        name="risk_review_outcome_01d9fd_idx",
                    )
                ],
            },
        ),
        migrations.AddConstraint(
            model_name="fundingriskreview",
            constraint=models.CheckConstraint(
                condition=models.Q(outcome__in=["APPROVED", "REJECTED"]),
                name="risk_funding_review_outcome_is_known",
            ),
        ),
        migrations.AddConstraint(
            model_name="fundingriskreview",
            constraint=models.CheckConstraint(
                condition=~models.Q(command_id=""),
                name="risk_funding_review_command_id_not_empty",
            ),
        ),
    ]
