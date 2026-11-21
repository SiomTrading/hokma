"""SPEC-015 section 16 (10.1): one test per calculation that Hokma 1.0.9 keeps (16.12 historical volatility, 16.15 market
hours, 16.16 portfolio greeks, 16.17 exposure and day result, 16.18 account activity), with the values worked out by hand
in each test. Option positioning analytics are not part of this version. No network, no clock. All identifiers are made up."""
import json
from datetime import datetime

import pytest

from hokma.schwab import analytics as an


def _candles(closes):
    return [{"open": c, "high": c, "low": c, "close": c, "volume": 1, "datetime": 1760000000000 + i * 86400000}
            for i, c in enumerate(closes)]


# ---------------------------------------------------------------------------------------------- 16.12

def test_16_12_historical_volatility_of_a_known_series():
    # Closes 100, 110, 99, 108.9: returns ln1.1 = .0953102, ln.9 = -.1053605, ln1.1 = .0953102; mean .0284199.
    # Squared deviations .0044743 + .0178971 + .0044743 = .0268457; / (3 - 1) = .0134228; sqrt = .115857;
    # times sqrt(252) = 15.8745 -> 1.83918.
    assert an.historical_volatility(_candles([100, 110, 99, 108.9]), 4) == pytest.approx(1.83918, abs=1e-4)
    assert an.historical_volatility(_candles([100, 110, 99]), 4) is None              # fewer candles than n


def test_16_12_volatility_history_windows_and_no_implied_volatility():
    # 30 candles growing 1 % a day: every log return is equal, so HV_30 = 0.
    steady = _candles([100 * 1.01 ** i for i in range(30)])
    out = an.volatility_history(list(reversed(steady)))                               # unsorted input is sorted
    assert out == {"hv": {"10": pytest.approx(0.0, abs=1e-6), "20": pytest.approx(0.0, abs=1e-6),
                          "30": pytest.approx(0.0, abs=1e-6), "60": None}}
    assert set(an.volatility_history(steady, windows=(5, 7))["hv"]) == {"5", "7"}


def test_the_positioning_analytics_are_not_in_this_version():
    assert not [n for n in dir(an) if n in {
        "Chain", "Contract", "chain_from_schwab", "chain_from_normalized", "normalize_chain", "options_analytics",
        "max_pain", "put_call", "term_structure", "skew", "expected_moves", "unusual_volume", "probabilities_itm",
        "iv_at_days"}]


# ---------------------------------------------------------------------------------------------- 16.15

def _day(date, regular=None, is_open=False):
    entry = {"date": date, "marketType": "EQUITY", "product": "EQ", "productName": "equity", "isOpen": is_open,
             "sessionHours": None if regular is None else {"regularMarket": [{"start": regular[0], "end": regular[1]}]}}
    return {"equity": {"EQ": entry}}


def _at(text):
    return datetime.fromisoformat(text).timestamp()


def test_16_15_market_open_closed_and_holiday():
    friday = _day("2026-11-20", ("2026-11-20T09:30:00-05:00", "2026-11-20T16:00:00-05:00"), True)
    # Open at 10:00: the interval contains now, the next change is its end (16:00 -05:00 = 21:00 UTC).
    out = an.market_hours(friday, _at("2026-11-20T10:00:00-05:00"))
    assert out["is_open"] is True and out["next_change"] == "2026-11-20T21:00:00Z" and out["next_change_kind"] == "close"
    # After the close, with only this day: closed and nothing later in the answer.
    late = an.market_hours(friday, _at("2026-11-20T17:00:00-05:00"))
    assert late["is_open"] is False and late["next_change"] is None
    # Thanksgiving (2026-11-26): isOpen false and no session hours. The next day closes early (13:00).
    holiday = _day("2026-11-26")
    after = _day("2026-11-27", ("2026-11-27T09:30:00-05:00", "2026-11-27T13:00:00-05:00"))
    closed = an.market_hours(holiday, _at("2026-11-26T10:00:00-05:00"))
    assert closed["is_open"] is False and closed["next_change"] is None
    both = an.market_hours([holiday, after], _at("2026-11-26T10:00:00-05:00"))
    assert both["is_open"] is False and both["next_change"] == "2026-11-27T14:30:00Z" and both["next_change_kind"] == "open"


# ---------------------------------------------------------------------------------------------- 16.16 and 16.17

def _position(symbol, asset, long=0.0, short=0.0, mv=0.0, day=0.0, lo=0.0, so=0.0, underlying=None):
    instrument = {"assetType": asset, "symbol": symbol}
    if underlying:
        instrument.update({"underlyingSymbol": underlying, "putCall": "CALL", "optionMultiplier": 100})
    return {"longQuantity": long, "shortQuantity": short, "marketValue": mv, "currentDayProfitLoss": day,
            "longOpenProfitLoss": lo, "shortOpenProfitLoss": so, "instrument": instrument}


OPTION_SYMBOL = "AAPL  261218C00100000"
ACCOUNTS = [
    {"securitiesAccount": {"type": "MARGIN", "accountNumber": "12345678", "positions": [
        _position("AAPL", "EQUITY", long=50, mv=5000, day=100, lo=200),
        _position(OPTION_SYMBOL, "OPTION", short=2, mv=-1000, day=-20, so=300, underlying="AAPL")]}},
    {"securitiesAccount": {"type": "CASH", "accountNumber": "87654321", "positions": [
        _position("MSFT", "EQUITY", long=10, mv=4000, day=50, lo=150)]}},
]
QUOTES = {OPTION_SYMBOL: {"assetMainType": "OPTION", "symbol": OPTION_SYMBOL,
                          "quote": {"delta": .5, "gamma": .04, "theta": -.08, "vega": .2},
                          "reference": {"multiplier": 100.0, "contractType": "C"}}}


def test_16_16_portfolio_greeks_stock_and_short_option():
    # Stock: delta = 50 (gamma, theta, vega 0). Short 2 calls, m = 100, net = -2:
    # delta -2*100*.5 = -100; gamma -2*100*.04 = -8; theta -2*100*(-.08) = +16; vega -2*100*.2 = -40.
    # AAPL: delta 50 - 100 = -50. MSFT 10 shares: delta 10. Account A1: -50; A2: 10; total -40.
    out = an.portfolio_greeks(ACCOUNTS, QUOTES, {"12345678": "A1"})
    option = next(p for p in out["positions"] if p["asset_type"] == "OPTION")
    assert (option["delta"], option["gamma"], option["theta"], option["vega"]) == (-100, -8, 16, -40)
    assert out["by_underlying"]["AAPL"] == {"delta": -50, "gamma": -8, "theta": 16, "vega": -40}
    assert out["by_underlying"]["MSFT"] == {"delta": 10, "gamma": 0, "theta": 0, "vega": 0}
    assert out["by_account"]["A1"]["delta"] == -50 and out["by_account"]["A2"]["delta"] == 10
    assert out["total"] == {"delta": -40, "gamma": -8, "theta": 16, "vega": -40}
    assert out["missing_greeks"] == []
    assert "12345678" not in json.dumps(out) and "87654321" not in json.dumps(out)


def test_16_16_option_without_a_quote_is_reported_not_guessed():
    out = an.portfolio_greeks(ACCOUNTS, {})
    assert out["missing_greeks"] == [OPTION_SYMBOL]
    assert out["total"]["delta"] == 60                                                # only the shares count


def test_16_17_exposure_and_day_result():
    # Account A1: market values 5000 and -1000 (sum 4000): weights 1.25 and -0.25; day 100 - 20 = 80;
    # open (200 + 0) + (0 + 300) = 500. A2: 4000 (weight 1), day 50, open 150.
    # All: sum 8000: weights .625, -.125, .5; day 130; open 650.
    out = an.exposure_and_pnl(ACCOUNTS)
    a1 = out["by_account"]["A1"]
    assert {w["symbol"]: w["weight"] for w in a1["weights"]} == {"AAPL": 1.25, OPTION_SYMBOL: -0.25}
    assert (a1["day_pnl"], a1["open_pnl"]) == (80, 500)
    assert out["by_account"]["A2"]["weights"][0]["weight"] == 1.0
    total = out["total"]
    assert {w["symbol"]: w["weight"] for w in total["weights"]} == {"AAPL": .625, OPTION_SYMBOL: -.125, "MSFT": .5}
    assert (total["market_value"], total["day_pnl"], total["open_pnl"]) == (8000, 130, 650)


# ---------------------------------------------------------------------------------------------- 16.18

def test_16_18_account_activity_fees_by_type_and_average_price():
    # netAmount by type: TRADE -1005 + 2000 = 995; DIVIDEND_OR_INTEREST 12.5.
    # Fees (items with feeType): COMMISSION -0.5 + -0.5 = -1.0; SEC_FEE -0.01 (the trade item has no feeType).
    # Order 111: legs 10 x 3, 11 x 1 and 12 x 4: (30 + 11 + 48) / 8 = 11.125. Order 222: nothing executed -> None.
    transactions = [
        {"type": "TRADE", "netAmount": -1005.0, "accountNumber": "12345678", "transferItems": [
            {"instrument": {"symbol": "XYZ"}, "amount": 10, "cost": -1000.0, "price": 100.0},
            {"feeType": "COMMISSION", "cost": -0.5}, {"feeType": "SEC_FEE", "cost": -0.01}]},
        {"type": "TRADE", "netAmount": 2000.0, "transferItems": [{"feeType": "COMMISSION", "cost": -0.5}]},
        {"type": "DIVIDEND_OR_INTEREST", "netAmount": 12.5},
    ]
    orders = [
        {"orderId": 111, "orderActivityCollection": [
            {"activityType": "EXECUTION", "executionLegs": [{"price": 10.0, "quantity": 3.0}, {"price": 11.0, "quantity": 1.0}]},
            {"activityType": "EXECUTION", "executionLegs": [{"price": 12.0, "quantity": 4.0}]}]},
        {"orderId": 222},
    ]
    out = an.account_activity(transactions, orders)
    assert out["net_by_type"] == {"TRADE": 995.0, "DIVIDEND_OR_INTEREST": 12.5}
    assert out["fees_by_type"] == {"COMMISSION": -1.0, "SEC_FEE": -0.01}
    assert out["fees_total"] == pytest.approx(-1.01)
    assert out["orders"][0] == {"order_id": 111, "filled_quantity": 8.0, "avg_execution_price": 11.125}
    assert out["orders"][1]["avg_execution_price"] is None
    assert "12345678" not in json.dumps(out)


# ---------------------------------------------------------------------------------------------- units

def test_no_data_sentinel_is_none_and_the_unit_is_the_one_market_options_reads():
    assert an._clean(-999.0) is None and an._clean(-1000) is None and an._clean(0.5) == 0.5
    assert an.VOLATILITY_UNIT == "percent"
