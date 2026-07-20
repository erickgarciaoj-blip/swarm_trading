"""Unit tests for SwarmSettings' risk-loss-limit validators (ADR-0010)."""

import pytest
from pydantic import ValidationError

from swarm_trading.core.config import SwarmSettings


def test_default_settings_are_valid():
    settings = SwarmSettings()
    assert settings.risk_max_daily_loss_pct == pytest.approx(0.15)
    assert settings.risk_max_total_loss_pct == pytest.approx(0.30)


@pytest.mark.parametrize("field_name", ["risk_max_daily_loss_pct", "risk_max_total_loss_pct"])
@pytest.mark.parametrize("bad_value", [0, -0.1, 1, 1.5, 100, -5])
def test_loss_pct_out_of_range_rejected(field_name, bad_value):
    with pytest.raises(ValidationError):
        SwarmSettings(**{field_name: bad_value})


def test_daily_loss_pct_greater_than_total_rejected():
    with pytest.raises(ValidationError):
        SwarmSettings(risk_max_daily_loss_pct=0.40, risk_max_total_loss_pct=0.30)


def test_daily_loss_pct_equal_to_total_is_allowed():
    settings = SwarmSettings(risk_max_daily_loss_pct=0.30, risk_max_total_loss_pct=0.30)
    assert settings.risk_max_daily_loss_pct == settings.risk_max_total_loss_pct
