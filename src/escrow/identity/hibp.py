"""Privacy-preserving Have I Been Pwned password range checks."""

from __future__ import annotations

import hashlib
from urllib.error import URLError
from urllib.request import Request, urlopen

from django.conf import settings


class HIBPUnavailableError(RuntimeError):
    """The live breach check could not complete and must fail closed."""


def password_is_pwned(password: str) -> bool:
    """Check a password without sending it or its full SHA-1 digest to HIBP."""
    if settings.HIBP_MODE == "mock":
        configured_passwords = {
            candidate for candidate in settings.HIBP_MOCK_PWNED_PASSWORDS.split("\n") if candidate
        }
        return password in configured_passwords

    digest = hashlib.sha1(password.encode("utf-8"), usedforsecurity=False).hexdigest().upper()
    prefix, suffix = digest[:5], digest[5:]
    request = Request(
        f"https://api.pwnedpasswords.com/range/{prefix}",
        headers={"Add-Padding": "true", "User-Agent": "escrow-portfolio-simulation"},
    )
    try:
        with urlopen(request, timeout=settings.HIBP_TIMEOUT_SECONDS) as response:  # noqa: S310
            result = response.read().decode("utf-8")
    except (OSError, TimeoutError, URLError) as error:
        raise HIBPUnavailableError from error
    return any(line.partition(":")[0] == suffix for line in result.splitlines())
