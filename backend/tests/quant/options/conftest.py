"""Fixtures of the options module tests: the recorded chain, no network, registry restored after a reset."""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from hokma.quant import registry
from hokma.quant.options import _common as c

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
OPTIONS_MODULES = (
    "hokma.quant.options.pricing", "hokma.quant.options.iv", "hokma.quant.options.greeks", "hokma.quant.options.trees",
    "hokma.quant.options.payoff", "hokma.quant.options.surface", "hokma.quant.options.position_greeks", "hokma.quant.options.vol_analysis",
    "hokma.quant.options.liquidity", "hokma.quant.options.structures", "hokma.quant.options.exotic", "hokma.quant.options.chain",
)


@pytest.fixture(autouse=True)
def _options_registered(monkeypatch):
    """Other test files reset the registry; reload the options modules when that happened. Calls run in this process."""
    monkeypatch.setattr(registry, "IN_PROCESS", True)
    if not registry.list("options"):
        for name in OPTIONS_MODULES:
            importlib.reload(importlib.import_module(name))
    yield


@pytest.fixture(scope="session")
def chain() -> dict:
    """The recorded synthetic SPY chain: spot, rate, dividend yield, five expiries of calls and puts, and daily closes."""
    return json.loads((FIXTURES / "option_chain_spy.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def atm(chain: dict) -> dict:
    """Inputs of the at-the-money call of the 2026-12-18 expiry, read from the chain."""
    row = next(r for r in chain["contracts"] if r["expiration"] == "2026-12-18" and r["type"] == "call" and r["strike"] == 590.0)
    t = c.years_between(chain["as_of"], row["expiration"])
    return {"spot": chain["underlying_price"], "strike": row["strike"], "rate": chain["rate"], "dividend_yield": chain["dividend_yield"],
            "volatility": row["iv"], "time_to_expiration": t, "row": row}


def call(name: str, **kwargs):
    return registry.call(name, **kwargs)


def piece_ids(name: str) -> tuple[str, ...]:
    return registry.describe(name).piece_ids


def assert_empty(result, *fragments: str) -> None:
    """A result that could not be computed: value None and the reason in warnings (never an exception)."""
    assert result.value is None
    assert result.warnings, "an empty result must explain itself"
    text = " ".join(result.warnings)
    for fragment in fragments:
        assert fragment in text, (fragment, text)
