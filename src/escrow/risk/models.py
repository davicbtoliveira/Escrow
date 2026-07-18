"""Stored policy snapshots and explainable funding-risk outcomes."""

from __future__ import annotations

import uuid

from django.db import models

from escrow.payments.models import Transfer


class FundingRiskPolicy(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    version = models.CharField(max_length=64, unique=True)
    configuration = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]


class FundingRiskDecision(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    transfer = models.OneToOneField(
        Transfer,
        on_delete=models.PROTECT,
        related_name="funding_risk_decision",
    )
    policy = models.ForeignKey(
        FundingRiskPolicy,
        on_delete=models.PROTECT,
        related_name="decisions",
    )
    policy_version = models.CharField(max_length=64)
    inputs = models.JSONField()
    score = models.PositiveSmallIntegerField()
    reasons = models.JSONField(default=list)
    outcome = models.CharField(max_length=32)
    evaluated_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["evaluated_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(score__gte=0) & models.Q(score__lte=100),
                name="risk_funding_score_between_0_and_100",
            ),
            models.CheckConstraint(
                condition=models.Q(outcome__in=["APPROVED", "REVIEW_REQUIRED", "REJECTED"]),
                name="risk_funding_outcome_is_known",
            ),
        ]
