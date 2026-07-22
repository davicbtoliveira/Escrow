"""Stored policy snapshots and explainable funding-risk outcomes."""

from __future__ import annotations

import uuid

from django.conf import settings
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
    policy_configuration = models.JSONField()
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


class FundingRiskReview(models.Model):
    """One attributable human resolution of a policy-required funding review."""

    class Outcome(models.TextChoices):
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    decision = models.OneToOneField(
        FundingRiskDecision,
        on_delete=models.PROTECT,
        related_name="manual_review",
    )
    analyst = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="funding_risk_reviews",
    )
    command_id = models.CharField(max_length=128, unique=True)
    outcome = models.CharField(max_length=16, choices=Outcome.choices)
    rationale = models.TextField(max_length=1_000)
    reviewed_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["reviewed_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(outcome__in=["APPROVED", "REJECTED"]),
                name="risk_funding_review_outcome_is_known",
            ),
            models.CheckConstraint(
                condition=~models.Q(command_id=""),
                name="risk_funding_review_command_id_not_empty",
            ),
        ]
        indexes = [
            models.Index(
                fields=["outcome", "reviewed_at"],
                name="risk_review_outcome_01d9fd_idx",
            )
        ]


class DisputeRiskPolicy(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    version = models.CharField(max_length=64, unique=True)
    configuration = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]


class DisputeRiskReport(models.Model):
    """An explainable risk evaluation generated for an opened dispute."""

    class SuspicionResult(models.TextChoices):
        NO_SUSPICION = "NO_SUSPICION", "No suspicion"
        SUSPICIOUS_INDICATORS = "SUSPICIOUS_INDICATORS", "Suspicious indicators"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    dispute = models.OneToOneField(
        "disputes.Dispute",
        on_delete=models.PROTECT,
        related_name="risk_report",
    )
    policy = models.ForeignKey(
        DisputeRiskPolicy,
        on_delete=models.PROTECT,
        related_name="reports",
    )
    policy_version = models.CharField(max_length=64)
    policy_configuration = models.JSONField(default=dict)
    inputs = models.JSONField(default=dict)
    summary = models.TextField()
    timeline = models.JSONField(default=list)
    customer_history = models.JSONField(default=dict)
    organization_history = models.JSONField(default=dict)
    evidence_integrity = models.JSONField(default=dict)
    score = models.PositiveSmallIntegerField()
    flags = models.JSONField(default=list)
    suspicion_result = models.CharField(
        max_length=32,
        choices=SuspicionResult.choices,
        default=SuspicionResult.NO_SUSPICION,
    )
    generated_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["generated_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(score__gte=0) & models.Q(score__lte=100),
                name="risk_dispute_score_between_0_and_100",
            ),
            models.CheckConstraint(
                condition=models.Q(suspicion_result__in=["NO_SUSPICION", "SUSPICIOUS_INDICATORS"]),
                name="risk_dispute_suspicion_result_is_known",
            ),
        ]

