"""Session-authenticated identity endpoints with explicit security boundaries."""

from __future__ import annotations

from typing import Any

from django.contrib.auth import authenticate, get_user_model, login, logout
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from escrow.http import InvalidJsonBody, error_response, parse_json_body
from escrow.identity.emails import EmailDeliveryError, send_password_recovery_email
from escrow.identity.hibp import HIBPUnavailableError, password_is_pwned
from escrow.identity.models import User
from escrow.organizations.models import Organization, OrganizationMember


def _string(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    return value.strip() if isinstance(value, str) else ""


def _password_errors(
    password: str, confirmation: str, *, user: User | None = None
) -> dict[str, list[str]]:
    errors: dict[str, list[str]] = {}
    if password != confirmation:
        errors["password_confirmation"] = ["As senhas não coincidem."]
        return errors
    try:
        validate_password(password, user)
    except ValidationError as error:
        errors["password"] = error.messages
        return errors
    if password_is_pwned(password):
        errors["password"] = ["Esta senha aparece em vazamentos conhecidos. Escolha outra."]
    return errors


@require_GET
@ensure_csrf_cookie
def csrf_token(request: HttpRequest) -> JsonResponse:
    """Issue the cookie/token pair required for session-mutating API requests."""
    return JsonResponse({"csrfToken": get_token(request)})


@require_POST
@csrf_protect
def register(request: HttpRequest) -> HttpResponse:
    """Create a human owner, organization, and membership in one transaction."""
    try:
        payload = parse_json_body(request)
    except InvalidJsonBody:
        return error_response("invalid_json", 400)
    email = _string(payload, "email").casefold()
    organization_name = _string(payload, "organization_name")
    password = _string(payload, "password")
    confirmation = _string(payload, "password_confirmation")
    errors: dict[str, list[str]] = {}
    if not email:
        errors["email"] = ["Informe um e-mail."]
    else:
        try:
            validate_email(email)
        except ValidationError:
            errors["email"] = ["Informe um e-mail válido."]
    if not organization_name:
        errors["organization_name"] = ["Informe o nome da organização."]
    try:
        errors.update(_password_errors(password, confirmation))
    except HIBPUnavailableError:
        return error_response(
            "hibp_unavailable",
            503,
            errors={
                "password": ["Não foi possível validar a senha contra vazamentos. Tente novamente."]
            },
        )
    if errors:
        return error_response("validation_error", 400, errors=errors)

    user_model = get_user_model()
    try:
        with transaction.atomic():
            user = user_model.objects.create_user(email=email, password=password)
            organization = Organization.objects.create(name=organization_name)
            membership = OrganizationMember.objects.create(
                organization=organization,
                user=user,
                role=OrganizationMember.Role.OWNER,
            )
    except IntegrityError:
        return error_response(
            "validation_error", 400, errors={"email": ["Este e-mail já está em uso."]}
        )
    login(request, user)
    return JsonResponse(
        {
            "user": {"id": str(user.id), "email": user.email},
            "organization": {"id": str(organization.id), "name": organization.name},
            "membership": {"id": str(membership.id), "role": membership.role},
        },
        status=201,
    )


@require_POST
@csrf_protect
def sign_in(request: HttpRequest) -> HttpResponse:
    """Authenticate with an email/password pair and establish a server session."""
    try:
        payload = parse_json_body(request)
    except InvalidJsonBody:
        return error_response("invalid_json", 400)
    email = _string(payload, "email").casefold()
    password = _string(payload, "password")
    user = authenticate(request, email=email, password=password)
    if user is None:
        return error_response("invalid_credentials", 401)
    login(request, user)
    return JsonResponse({"user": {"id": str(user.id), "email": user.email}})


@require_POST
@csrf_protect
def sign_out(request: HttpRequest) -> HttpResponse:
    """End the current server-side session."""
    logout(request)
    return HttpResponse(status=204)


@require_POST
@csrf_protect
def password_recovery(request: HttpRequest) -> HttpResponse:
    """Send a reset capability without revealing whether the address is registered."""
    try:
        payload = parse_json_body(request)
    except InvalidJsonBody:
        return error_response("invalid_json", 400)
    email = _string(payload, "email").casefold()
    user = get_user_model().objects.filter(email=email, is_active=True).first()
    if user is not None:
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        try:
            send_password_recovery_email(user, uid, token)
        except EmailDeliveryError:
            return error_response("password_recovery_unavailable", 503)
    return JsonResponse({"status": "accepted"}, status=202)


@require_POST
@csrf_protect
def confirm_password_recovery(request: HttpRequest) -> HttpResponse:
    """Validate the emailed capability and replace the password safely."""
    try:
        payload = parse_json_body(request)
    except InvalidJsonBody:
        return error_response("invalid_json", 400)
    uid = _string(payload, "uid")
    token = _string(payload, "token")
    try:
        user_id = force_str(urlsafe_base64_decode(uid))
        user = get_user_model().objects.get(pk=user_id, is_active=True)
    except (TypeError, ValueError, OverflowError, get_user_model().DoesNotExist):
        return error_response("invalid_reset_token", 400)
    if not default_token_generator.check_token(user, token):
        return error_response("invalid_reset_token", 400)
    password = _string(payload, "password")
    confirmation = _string(payload, "password_confirmation")
    try:
        errors = _password_errors(password, confirmation, user=user)
    except HIBPUnavailableError:
        return error_response(
            "hibp_unavailable",
            503,
            errors={
                "password": ["Não foi possível validar a senha contra vazamentos. Tente novamente."]
            },
        )
    if errors:
        return error_response("validation_error", 400, errors=errors)
    user.set_password(password)
    user.save(update_fields=["password"])
    return JsonResponse({"status": "password_updated"})
