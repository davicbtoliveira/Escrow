"""Top-level HTTP routes kept outside financial bounded contexts."""

from __future__ import annotations

from django.urls import include, path
from drf_spectacular.views import SpectacularJSONAPIView

from escrow.agreements import views as agreement_views
from escrow.delivery import views as delivery_views
from escrow.disputes import views as dispute_views
from escrow.health import liveness, readiness
from escrow.payments import views as payment_views

urlpatterns = [
    path("health/live/", liveness, name="health-live"),
    path("health/ready/", readiness, name="health-ready"),
    path("api/v1/auth/", include("escrow.identity.urls")),
    path("api/v1/integrations/", include("escrow.integrations.urls")),
    path("api/v1/operations/risk/", include("escrow.risk.urls")),
    path("api/v1/organizations/", include("escrow.organizations.urls")),
    path("api/v1/agreements/", agreement_views.agreement_collection, name="agreement-collection"),
    path(
        "api/v1/agreements/<uuid:agreement_id>/delivery/",
        delivery_views.report_agreement_delivery,
        name="agreement-delivery-report",
    ),
    path(
        "api/v1/checkout/<str:checkout_token>/pix-charges/",
        payment_views.public_checkout_pix_charge,
        name="public-checkout-pix-charge",
    ),
    path(
        "api/v1/checkout/<str:checkout_token>/delivery-acceptance/otp/",
        delivery_views.request_customer_delivery_acceptance_otp,
        name="customer-delivery-otp-request",
    ),
    path(
        "api/v1/checkout/<str:checkout_token>/delivery-acceptance/otp/<uuid:challenge_id>/verify/",
        delivery_views.verify_customer_delivery_acceptance_otp,
        name="customer-delivery-otp-verify",
    ),
    path(
        "api/v1/checkout/<str:checkout_token>/delivery-acceptance/",
        delivery_views.accept_customer_reported_delivery,
        name="customer-delivery-acceptance",
    ),
    path(
        "api/v1/checkout/<str:checkout_token>/disputes/otp/",
        dispute_views.request_customer_dispute_otp,
        name="customer-dispute-otp-request",
    ),
    path(
        "api/v1/checkout/<str:checkout_token>/disputes/otp/<uuid:challenge_id>/verify/",
        dispute_views.verify_customer_dispute_otp,
        name="customer-dispute-otp-verify",
    ),
    path(
        "api/v1/checkout/<str:checkout_token>/disputes/",
        dispute_views.open_customer_dispute_view,
        name="customer-dispute-open",
    ),
    path(
        "api/v1/checkout/<str:checkout_token>/disputes/<uuid:dispute_id>/evidence/",
        dispute_views.upload_customer_dispute_evidence,
        name="customer-dispute-evidence-upload",
    ),
    path(
        "api/v1/checkout/<str:checkout_token>/",
        agreement_views.public_checkout,
        name="public-checkout",
    ),
    path(
        "api/v1/providers/sandbox-pix/callbacks/",
        payment_views.sandbox_pix_callback,
        name="sandbox-pix-callback",
    ),
    path(
        "api/v1/sandbox/pix/charges/<uuid:charge_id>/actions/",
        payment_views.sandbox_pix_control,
        name="sandbox-pix-control",
    ),
    path("api/v1/openapi.json", SpectacularJSONAPIView.as_view(), name="openapi-schema"),
]
