"""
Regression tests for core/models.py's domain enums after the (str, Enum) ->
StrEnum migration (Fase 2 mypy pass, ruff UP042). StrEnum and (str, Enum) are
behaviorally near-identical for how this codebase uses them (.value, JSON
serialization, string equality) but the migration touched every enum used
throughout the app — worth locking down explicitly rather than relying on
the rest of the suite to notice a regression incidentally.
"""

import json
from enum import StrEnum

from swarm_trading.core.models import (
    AgentStatus,
    AgentType,
    NewsImpact,
    OrderStatus,
    Side,
    Symbol,
)

ALL_DOMAIN_ENUMS = [Symbol, Side, AgentType, AgentStatus, OrderStatus, NewsImpact]


def test_all_domain_enums_are_strenum():
    for enum_cls in ALL_DOMAIN_ENUMS:
        assert issubclass(enum_cls, StrEnum)


def test_str_conversion_yields_the_bare_value_not_repr():
    # This is the exact property that motivated picking StrEnum over plain
    # (str, Enum): str(x) is guaranteed to be the value, not "Symbol.XAUUSD".
    assert str(Symbol.XAUUSD) == "XAUUSD"
    assert f"{Side.LONG}" == "LONG"


def test_equality_and_membership_against_plain_strings():
    assert Symbol.XAUUSD == "XAUUSD"
    assert Symbol("XAUUSD") is Symbol.XAUUSD
    assert "XAUUSD" in {s.value for s in Symbol}


def test_json_serialization_matches_value_with_or_without_dot_value():
    # dashboard/api/routes.py and the WebSocket payloads serialize some
    # fields via `.value` explicitly and rely on plain json.dumps for
    # others (e.g. inside dicts already built with .value) — both paths
    # must produce the same plain string, not "Symbol.XAUUSD".
    assert json.dumps({"symbol": Symbol.XAUUSD.value}) == '{"symbol": "XAUUSD"}'
    assert json.dumps({"symbol": Symbol.XAUUSD}) == '{"symbol": "XAUUSD"}'


def test_all_five_trading_symbols_present():
    assert {s.value for s in Symbol} == {"XAUUSD", "PLTR", "NAS100", "US100", "OIL"}
