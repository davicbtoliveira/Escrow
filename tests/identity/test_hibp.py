from __future__ import annotations

import hashlib
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from escrow.identity.hibp import password_is_pwned


class HIBPPasswordRangeTests(SimpleTestCase):
    @override_settings(HIBP_MODE="live")
    @patch("escrow.identity.hibp.urlopen")
    def test_live_check_sends_only_the_sha1_prefix_with_padding(self, urlopen: MagicMock) -> None:
        password = "Uma senha forte e exclusiva 2026!"
        digest = hashlib.sha1(password.encode("utf-8"), usedforsecurity=False).hexdigest().upper()
        response = MagicMock()
        response.read.return_value = f"{digest[5:]}:42\nABC:1\n".encode()
        urlopen.return_value.__enter__.return_value = response

        assert password_is_pwned(password)

        request = urlopen.call_args.args[0]
        assert digest[:5] in request.full_url
        assert digest[5:] not in request.full_url
        assert password not in request.full_url
        assert request.headers["Add-padding"] == "true"
