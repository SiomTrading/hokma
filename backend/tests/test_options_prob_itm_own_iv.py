"""Prob. ITM with the implied volatility of each contract's own mark, against thinkorswim (tarea 3330, version 1.0.14)."""
from __future__ import annotations

import json
import math
from datetime import date, datetime
from pathlib import Path

import pytest

from hokma.market import options
from hokma.market.tz import NEW_YORK

FIXTURE = json.loads((Path(__file__).parent / "options_fixtures" / "tos_prob_itm_23nov.json").read_text())
NOW = datetime(2026, 11, 23, 9, 25, tzinfo=NEW_YORK).timestamp()


def _cases():
    for day, rows in FIXTURE["expirations"].items():
        for strike, cb, ca, cp, pb, pa, pp in rows:
            yield day, strike, "call", cb, ca, cp
            yield day, strike, "put", pb, pa, pp


def _got(day, strike, kind, bid, ask):
    years = options.years_to_expiry(date.fromisoformat(day), NOW)
    return options.contract_prob_itm(kind, FIXTURE["underlying_bid"], strike, bid, ask, 0.5, years) * 100.0


def test_every_row_is_thinkorswims_within_a_hundredth_of_a_point():
    errors = [abs(_got(day, strike, kind, bid, ask) - tos) for day, strike, kind, bid, ask, tos in _cases()]
    assert len(errors) == 36 and max(errors) < 0.015


def test_the_call_and_the_put_of_one_strike_can_add_up_to_more_than_100():
    years = options.years_to_expiry(date(2026, 11, 23), NOW)
    total = (options.contract_prob_itm("call", 654.68, 648, 7.5, 11.59, 0.5, years) + options.contract_prob_itm("put", 654.68, 648, 3.0, 8.0, 0.5, years))
    assert total == pytest.approx(1.0625, abs=0.001)


def test_mark_is_the_midpoint_and_the_ask_over_two_without_bid():
    assert options.mark_price(7.5, 11.59) == pytest.approx(9.545) and options.mark_price(0, 7.0) == 3.5 and options.mark_price(3.0, None) is None and options.mark_price(3.0, 0) is None


def test_without_a_valid_ask_it_falls_back_to_the_iv_schwab_sends():
    years = options.years_to_expiry(date(2026, 11, 24), NOW)
    assert options.contract_prob_itm("call", 654.68, 650, 6.5, None, 0.55, years) == options.prob_itm("call", 654.68, 650, 0.55, years)
    assert options.contract_prob_itm("call", 654.68, 650, 6.5, 0, 0.55, years) == options.prob_itm("call", 654.68, 650, 0.55, years)


def test_a_mark_below_intrinsic_value_falls_back_to_schwabs_iv():
    years = options.years_to_expiry(date(2026, 11, 24), NOW)
    assert options.implied_vol_from_mark("call", 654.68, 600, 1.0, years) is None
    assert options.contract_prob_itm("call", 654.68, 600, 0.5, 1.5, 0.55, years) == options.prob_itm("call", 654.68, 600, 0.55, years)


def test_the_rate_is_the_single_named_constant():
    assert options.PROB_ITM_RATE == 0.04
    assert math.isclose(options.implied_vol_from_mark("call", 100, 100, options._bs_price("call", 100, 100, 0.3, 0.5, 0.04), 0.5), 0.3, abs_tol=1e-6)
