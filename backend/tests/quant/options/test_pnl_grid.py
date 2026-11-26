"""options.pnl_grid (SPEC-028, 3339-A): the table of profit and loss by date and price, against references that do not use the module's own pricing:
a Black-Scholes written here with math.erf, put-call parity, the Hull textbook values, and the payoff of the library at expiration."""
import math

import numpy as np
import pytest

from hokma.quant.options import detail, payoff as pay
from hokma.quant.options.structures import build_legs

SPOT, YEARS, RATE = 100.0, 30 / 365, 0.04


def ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def ref_bs(s, k, r, v, t, put, q=0.0):
    if t <= 0:
        return max(k - s, 0.0) if put else max(s - k, 0.0)
    d1 = (math.log(s / k) + (r - q + v * v / 2) * t) / (v * math.sqrt(t))
    d2 = d1 - v * math.sqrt(t)
    if put:
        return k * math.exp(-r * t) * ncdf(-d2) - s * math.exp(-q * t) * ncdf(-d1)
    return s * math.exp(-q * t) * ncdf(d1) - k * math.exp(-r * t) * ncdf(d2)


def leg(position, kind, strike, premium, quantity=1):
    return {"position": position, "kind": kind, "strike": strike, "premium": premium, "quantity": quantity}


def grid(legs, ivs, **kw):
    args = dict(spot=SPOT, years=YEARS, multiplier=100, iv_by_leg=ivs, rate=RATE)
    args.update(kw)
    res = detail.pnl_grid(legs, **args)
    assert res.value is not None, res.warnings
    return res.value


BULL = [leg("long", "call", 100.0, 3.2), leg("short", "call", 110.0, 0.9)]


def test_hull_textbook_prices_in_the_table():
    """S=42, K=40, r=10 %, vol 20 %, six months: call 4.7594 and put 0.8086 (Hull). A long option with no premium reads its price per share."""
    for kind, expected in (("call", 4.7594), ("put", 0.8086)):
        v = grid([leg("long", kind, 40.0, 0.0)], [0.20], spot=42.0, years=0.5, multiplier=1, rate=0.10, days=[0, 182.5])
        mid = v["spot_row"]
        assert v["rows"][mid]["price"] == 42.0 and v["rows"][mid]["current"] is True
        assert v["pnl_usd"][mid][0] == pytest.approx(expected, abs=5e-5)


def test_every_cell_before_expiration_is_the_black_scholes_of_each_leg():
    v = grid(BULL, [0.30, 0.27])
    for i, row in enumerate(v["rows"]):
        for j, col in enumerate(v["columns"]):
            if col["expiration"]:
                continue
            t = YEARS - col["days"] / 365
            want = sum(sign * (ref_bs(row["price"], k, RATE, iv, t, False) - prem) * 100 for sign, k, iv, prem in ((1, 100.0, 0.30, 3.2), (-1, 110.0, 0.27, 0.9)))
            assert v["pnl_usd"][i][j] == pytest.approx(want, abs=1e-8)


def test_the_column_of_expiration_is_the_payoff_of_the_library_at_each_price():
    v = grid(BULL, [0.30, 0.27])
    last = [row[-1] for row in v["pnl_usd"]]
    legs = pay.parse_legs(BULL, 100)
    assert last == pytest.approx(pay.pnl_at(legs, np.array([r["price"] for r in v["rows"]])).tolist(), abs=1e-9)
    assert v["columns"][-1]["expiration"] is True and v["columns"][0]["days"] == 0.0


def test_put_call_parity_holds_for_the_whole_table():
    """Long call and short put at one strike and one volatility is a forward: C - P = S - K exp(-rT) on every date, and S - K at expiration."""
    legs = [leg("long", "call", 100.0, 0.0), leg("short", "put", 100.0, 0.0)]
    v = grid(legs, [0.35, 0.35], multiplier=1, days=[0, 10, 20, 30])
    for i, row in enumerate(v["rows"]):
        for j, col in enumerate(v["columns"]):
            t = max(YEARS - col["days"] / 365, 0.0) if not col["expiration"] else 0.0
            assert v["pnl_usd"][i][j] == pytest.approx(row["price"] - 100.0 * math.exp(-RATE * t), abs=1e-9)


def test_volatility_multiple_moves_the_dates_before_expiration_and_not_expiration():
    a, b = grid(BULL, [0.30, 0.27], iv_mult=1.0), grid(BULL, [0.30, 0.27], iv_mult=1.5)
    assert a["pnl_usd"] != b["pnl_usd"]
    assert [row[-1] for row in a["pnl_usd"]] == [row[-1] for row in b["pnl_usd"]]
    assert b["iv_by_leg"] == pytest.approx([0.45, 0.405])
    i = a["spot_row"]
    want = sum(sign * (ref_bs(100.0, k, RATE, iv * 1.5, YEARS, False) - prem) * 100 for sign, k, iv, prem in ((1, 100.0, 0.30, 3.2), (-1, 110.0, 0.27, 0.9)))
    assert b["pnl_usd"][i][0] == pytest.approx(want, abs=1e-8)


def test_a_range_of_three_percent_gives_thirteen_rows_from_minus_three_to_plus_three_with_the_current_price_exact():
    v = grid(BULL, [0.3, 0.27], range_pct=3)
    assert len(v["rows"]) == 13 and v["rows"][0]["pct"] == pytest.approx(-3.0) and v["rows"][-1]["pct"] == pytest.approx(3.0)
    assert v["rows"][0]["price"] == pytest.approx(97.0) and v["rows"][-1]["price"] == pytest.approx(103.0)
    assert [r["current"] for r in v["rows"]].count(True) == 1 and v["rows"][6]["current"] and v["rows"][6]["price"] == SPOT and v["spot_row"] == 6


def test_a_bull_call_spread_has_one_breakeven_and_an_iron_condor_two_and_they_are_exact():
    v = grid(BULL, [0.3, 0.27])
    assert v["breakevens"] == pytest.approx([100.0 + (3.2 - 0.9)])
    cond = build_legs("iron_condor", [90.0, 95.0, 105.0, 110.0], [0.4, 1.1, 1.3, 0.5], 1, 100, SPOT)
    dicts = [{"instrument": "option", "position": "long" if x.sign > 0 else "short", "kind": "put" if x.put else "call", "strike": x.strike, "premium": x.premium,
              "quantity": x.quantity, "multiplier": x.multiplier} for x in cond]
    w = grid(dicts, [0.3, 0.28, 0.27, 0.3], range_pct=20)
    credit = 1.1 + 1.3 - 0.4 - 0.5
    assert w["breakevens"] == pytest.approx([95.0 - credit, 105.0 + credit]) and w["metrics"]["premium_type"] == "credit"
    assert w["metrics"]["max_profit_usd"] == pytest.approx(credit * 100) and w["metrics"]["max_loss_usd"] == pytest.approx((5.0 - credit) * 100)


def test_the_curve_at_half_of_the_time_is_black_scholes_at_half_and_its_breakeven_prices_to_the_premium():
    legs = [leg("long", "call", 100.0, 3.0)]
    v = grid(legs, [0.30], multiplier=1, range_pct=20)
    cur = v["curves"]
    assert cur["mid_days"] == pytest.approx(YEARS * 365 / 2)
    t_mid = YEARS / 2
    for s, y in zip(cur["spots"][::10], cur["mid_pnl_usd"][::10]):
        assert y == pytest.approx(ref_bs(s, 100.0, RATE, 0.30, t_mid, False) - 3.0, abs=1e-9)
    assert len(cur["mid_breakevens"]) == 1
    assert ref_bs(cur["mid_breakevens"][0], 100.0, RATE, 0.30, t_mid, False) == pytest.approx(3.0, abs=2e-2)      # linear interpolation between samples
    assert cur["expiration_pnl_usd"] == pytest.approx(pay.pnl_at(pay.parse_legs(legs, 1), np.array(cur["spots"])).tolist())


def test_probability_and_expected_value_are_the_closed_forms_of_a_long_call():
    legs = [leg("long", "call", 100.0, 3.0)]
    v = grid(legs, [0.30], multiplier=1)
    m = v["metrics"]
    be = 103.0
    d2 = (math.log(SPOT / be) + (RATE - 0.30 ** 2 / 2) * YEARS) / (0.30 * math.sqrt(YEARS))
    assert m["p_profit"] == pytest.approx(ncdf(d2), abs=1e-12)
    assert m["expected_value_usd"] == pytest.approx(math.exp(RATE * YEARS) * ref_bs(SPOT, 100.0, RATE, 0.30, YEARS, False) - 3.0, abs=1e-9)
    assert m["net_premium_usd"] == 3.0 and m["premium_type"] == "debit" and m["max_loss_usd"] == 3.0 and m["unbounded_profit"] is True


def test_dividend_yield_is_zero_by_default_and_changes_the_price_when_given():
    base = grid([leg("long", "call", 100.0, 0.0)], [0.3], multiplier=1, days=[0, 10])
    with_q = grid([leg("long", "call", 100.0, 0.0)], [0.3], multiplier=1, days=[0, 10], dividend_yield=0.03)
    assert base["dividend_yield"] == 0.0 and with_q["pnl_usd"][6][0] == pytest.approx(ref_bs(100.0, 100.0, RATE, 0.3, YEARS, False, 0.03), abs=1e-9)
    assert base["pnl_usd"][6][0] != with_q["pnl_usd"][6][0]


def test_stock_legs_move_with_the_price_on_every_date_and_the_protective_put_floor_holds_at_expiration():
    legs = [{"instrument": "stock", "position": "long", "premium": 100.0, "quantity": 100}, leg("long", "put", 95.0, 1.5)]
    v = grid(legs, [0.3], range_pct=20)
    last = [row[-1] for row in v["pnl_usd"]]
    assert min(last) == pytest.approx(-(100.0 - 95.0 + 1.5) * 100) and v["metrics"]["max_loss_usd"] == pytest.approx(650.0)
    today = v["pnl_usd"][0][0]
    assert today == pytest.approx((80.0 - 100.0) * 100 + (ref_bs(80.0, 95.0, RATE, 0.3, YEARS, True) - 1.5) * 100, abs=1e-8)


def test_a_leg_without_volatility_leaves_the_dates_before_expiration_empty_and_expiration_whole():
    v = grid(BULL, [0.30, None])
    assert all(row[0] is None for row in v["pnl_usd"]) and all(row[-1] is not None for row in v["pnl_usd"])
    assert v["curves"]["mid_pnl_usd"] is None and v["metrics"]["max_profit_usd"] is not None
    res = detail.pnl_grid(BULL, SPOT, YEARS, multiplier=100, iv_by_leg=[0.3, None], rate=RATE)
    assert any("no volatility" in w for w in res.warnings)


def test_the_limits_of_the_grid_are_refused_with_their_reason():
    for kw, text in (({"range_pct": 2.9}, "range_pct"), ({"range_pct": 20.1}, "range_pct"), ({"iv_mult": 0.4}, "iv_mult"), ({"iv_mult": 2.1}, "iv_mult"),
                     ({"rows": 12}, "odd"), ({"rows": 63}, "odd"), ({"rows": 1}, "odd"), ({"days": list(range(13))}, "columns"), ({"curve_points": 999}, "curve_points"),
                     ({"days": [-1.0]}, "days"), ({"iv_by_leg": [0.3]}, "one entry per option leg")):
        args = dict(spot=SPOT, years=YEARS, multiplier=100, iv_by_leg=[0.3, 0.27], rate=RATE)
        args.update(kw)
        res = detail.pnl_grid(BULL, **args)
        assert res.value is None and text in " ".join(res.warnings), kw
    big = detail.pnl_grid(BULL, SPOT, YEARS, multiplier=100, iv_by_leg=[0.3, 0.27], rows=detail.MAX_ROWS, days=[float(d) for d in range(12)], curve_points=detail.MAX_CURVE_POINTS)
    assert big.value is not None and len(big.value["rows"]) == 61 and len(big.value["columns"]) == 12 and len(big.value["curves"]["spots"]) == 201


def test_expired_or_invalid_inputs_give_no_value_and_the_multiplier_has_no_default():
    assert detail.pnl_grid(BULL, SPOT, 0.0, multiplier=100, iv_by_leg=[0.3, 0.27]).value is None
    assert detail.pnl_grid(BULL, SPOT, YEARS, iv_by_leg=[0.3, 0.27]).value is None
    assert detail.pnl_grid([], SPOT, YEARS, multiplier=100).value is None
    assert detail.pnl_grid(BULL, SPOT, YEARS, multiplier=100, iv_by_leg=[0.3, 11.0], iv_mult=2.0).value is None


def test_the_columns_are_the_days_asked_and_a_day_past_expiration_is_expiration():
    v = grid(BULL, [0.3, 0.27], days=[0, 5, 15, 40])
    assert [c["expiration"] for c in v["columns"]] == [False, False, False, True] and v["columns"][-1]["days"] == pytest.approx(YEARS * 365)
    assert [c["years_left"] for c in v["columns"]][:3] == pytest.approx([YEARS, YEARS - 5 / 365, YEARS - 15 / 365])


def test_the_function_is_registered_and_runs_in_the_worker_with_the_same_numbers(monkeypatch):
    from hokma.quant import registry, runner

    spec = registry.describe("options.pnl_grid")
    assert spec.module == "options" and "FT-036" in spec.piece_ids and spec.inputs["multiplier"].get("default") is None
    monkeypatch.setattr(registry, "IN_PROCESS", False)
    try:
        res = registry.call("options.pnl_grid", legs=BULL, spot=SPOT, years=YEARS, multiplier=100, iv_by_leg=[0.3, 0.27], rate=RATE)
        assert res.value["breakevens"] == pytest.approx([102.3]) and res.value["pnl_usd"] == grid(BULL, [0.3, 0.27])["pnl_usd"]
    finally:
        runner.shutdown()


def test_the_mid_breakevens_are_marked_approximate_and_in_range_only():
    cur = grid([leg("long", "call", 100.0, 3.0)], [0.30], multiplier=1, range_pct=20)["curves"]
    assert cur["mid_breakevens_approximate"] is True and cur["mid_breakevens_in_range_only"] is True
    assert cur["mid_breakevens_range"] == pytest.approx([cur["spots"][0], cur["spots"][-1]])


def test_one_leg_without_volatility_leaves_probability_and_expected_value_empty_never_the_mean_of_the_others():
    res = detail.pnl_grid(BULL, SPOT, YEARS, multiplier=100, iv_by_leg=[0.3, None], rate=RATE)
    m = res.value["metrics"]
    assert m["p_profit"] is None and m["expected_value_usd"] is None and res.value["iv_mean"] is None
    assert m["max_profit_usd"] is not None
    assert any("probability of profit" in w for w in res.warnings)
    full = grid(BULL, [0.3, 0.27])["metrics"]
    assert full["p_profit"] is not None and full["expected_value_usd"] is not None


def test_days_are_clipped_to_the_term_before_they_are_deduplicated():
    total = YEARS * 365
    v = grid(BULL, [0.3, 0.27], days=[0.0, total, total + 5, total + 9])
    assert [c["days"] for c in v["columns"]] == pytest.approx([0.0, total])
