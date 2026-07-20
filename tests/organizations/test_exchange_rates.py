from __future__ import annotations

from datetime import timedelta

import pytest
from django.test import TestCase
from django.utils import timezone

from escrow.organizations.models import ExchangeRate
from escrow.organizations.services import latest_simulated_rate


@pytest.mark.django_db
def test_seeded_simulated_rates_cover_both_display_directions() -> None:
    brl_usd = latest_simulated_rate("BRL", "USD")
    usd_brl = latest_simulated_rate("USD", "BRL")

    assert brl_usd is not None
    assert brl_usd.is_simulated
    assert brl_usd.rate_micros > 0
    assert brl_usd.recorded_at is not None
    assert usd_brl is not None
    assert usd_brl.is_simulated


class LatestSimulatedRateTests(TestCase):
    def test_newest_record_wins_and_stays_scoped_to_its_pair(self) -> None:
        seeded = latest_simulated_rate("BRL", "USD")
        assert seeded is not None

        newer = ExchangeRate.objects.create(
            base_currency="BRL",
            quote_currency="USD",
            rate_micros=200_000,
            recorded_at=timezone.now() + timedelta(minutes=1),
        )

        assert latest_simulated_rate("BRL", "USD") == newer
        reverse = latest_simulated_rate("USD", "BRL")
        assert reverse is not None
        assert reverse.rate_micros != 200_000

    def test_unknown_pair_has_no_simulated_rate(self) -> None:
        assert latest_simulated_rate("BRL", "BRL") is None
