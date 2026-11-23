"""Expirations and chain of the module. Row FT-033: read from hokma.market.options, with the library only as a marked fallback."""
import pandas as pd
import pytest

from hokma.market import options as market_options
from hokma.quant.errors import BadInput
from hokma.quant.options import _lib, chain as chain_module

from .conftest import assert_empty, piece_ids


@pytest.fixture
def connected(monkeypatch, chain):
    """The market options module answers from the recorded chain (as Schwab would); it is replaced, not changed."""
    calls: dict[str, list] = {"chain": [], "expirations": []}

    def expirations(symbol, desk_id=None):
        calls["expirations"].append((symbol, desk_id))
        days = sorted({(r["expiration"], r["days"]) for r in chain["contracts"]})
        return {"symbol": symbol, "source": "Schwab", "expirations": [{"date": d, "days": n, "multiplier": 100} for d, n in days]}

    def options_chain(symbol, expiration=None, type=None, min_days=None, max_days=None, min_delta=None, max_delta=None, min_open_interest=None,
                      max_spread_pct=None, limit=None, desk_id=None):
        calls["chain"].append({"symbol": symbol, "expiration": expiration, "type": type, "min_days": min_days, "max_days": max_days, "limit": limit, "desk_id": desk_id})
        rows = [r for r in chain["contracts"] if (expiration is None or r["expiration"] == expiration) and (type is None or r["type"] == type)]
        return {"underlying": symbol, "underlying_price": chain["underlying_price"], "source": "Schwab", "price_feed": "schwab", "greeks": "provider",
                "as_of": chain["as_of"], "delayed": False, "contracts": rows[: (limit or 50)]}

    monkeypatch.setattr(market_options, "expirations", expirations)
    monkeypatch.setattr(market_options, "options_chain", options_chain)
    return calls


@pytest.fixture
def not_connected(monkeypatch):
    def refuse(*args, **kwargs):
        raise market_options.OptionsError("no_connection", 409, "no chain is connected")

    monkeypatch.setattr(market_options, "expirations", refuse)
    monkeypatch.setattr(market_options, "options_chain", refuse)


def test_dates_and_chain_come_from_the_market_module_FT_033(connected, chain):
    dates = chain_module.expiry_dates("spy")
    assert dates.value["source"] == "Schwab" and dates.value["delayed"] is False
    assert [e["date"] for e in dates.value["expirations"]] == ["2026-11-27", "2026-12-18", "2027-01-15", "2027-03-19", "2027-06-18"]
    assert dates.value["expirations"][0] == {"date": "2026-11-27", "days": 7, "multiplier": 100}
    assert connected["expirations"] == [("SPY", None)]
    res = chain_module.option_chain("SPY", expiration="2026-12-18", kind="put", limit=7, desk_id=3)
    assert res.value["source"] == "Schwab" and res.value["delayed"] is False and res.value["greeks"] == "provider"
    assert res.value["underlying_price"] == chain["underlying_price"]
    assert len(res.value["contracts"]) == 7 and all(r["type"] == "put" and r["expiration"] == "2026-12-18" for r in res.value["contracts"])
    assert connected["chain"][-1] == {"symbol": "SPY", "expiration": "2026-12-18", "type": "put", "min_days": None, "max_days": None, "limit": 7, "desk_id": 3}
    # the rows are what the other functions take
    from hokma.quant import registry

    full = chain_module.option_chain("SPY", expiration="2027-01-15", limit=200).value["contracts"]
    smile = registry.call("options.chain_liquidity", contracts=full)
    assert smile.value["n_valid"] == len(full)
    # a delayed answer of the connected source is passed on as delayed
    assert "FT-033" in piece_ids("options.expiry_dates") and "FT-033" in piece_ids("options.option_chain")


def test_the_library_answers_only_when_no_chain_is_connected_and_says_so_FT_033(not_connected, monkeypatch):
    seen = []

    def library_dates(symbol):
        seen.append(("dates", symbol))
        return ["2026-12-18", "2027-01-15"]

    table = pd.DataFrame({"Contract Symbol": ["SPY261218C00590000", "SPY261218C00600000"], "Bid": [10.0, 5.0], "Ask": [10.2, 5.2], "Last Price": [10.1, 5.1],
                          "Volume": [100, 50], "Open Interest": [1000, 800], "Implied Volatility": [0.16, 0.15]},
                         index=pd.MultiIndex.from_tuples([("SPY", 590.0), ("SPY", 600.0)], names=["Ticker", "Strike Price"]))

    def library_chain(tickers, expiration_date, put_option=False):
        seen.append(("chain", tickers, expiration_date, put_option))
        return table if not put_option else pd.DataFrame()

    monkeypatch.setattr(_lib.ft_options, "get_option_expiry_dates", library_dates)
    monkeypatch.setattr(_lib.ft_options, "get_option_chains", library_chain)
    dates = chain_module.expiry_dates("SPY")
    assert dates.value["source"] == "library" and dates.value["delayed"] is True
    assert dates.value["provider"] == "Yahoo Finance" and any("Yahoo Finance" in w for w in dates.warnings)      # the fallback names itself
    assert [e["date"] for e in dates.value["expirations"]] == ["2026-12-18", "2027-01-15"] and dates.value["expirations"][0]["multiplier"] is None
    assert any("no chain is connected" in w for w in dates.warnings)
    res = chain_module.option_chain("SPY", expiration="2026-12-18")
    assert res.value["source"] == "library" and res.value["delayed"] is True and res.value["greeks"] == "none"
    assert res.value["provider"] == "Yahoo Finance" and any("Yahoo Finance" in w for w in res.warnings)
    rows = res.value["contracts"]
    assert [(r["type"], r["strike"], r["bid"], r["ask"], r["open_interest"], r["iv"]) for r in rows] == [("call", 590.0, 10.0, 10.2, 1000.0, 0.16), ("call", 600.0, 5.0, 5.2, 800.0, 0.15)]
    assert rows[0]["mid"] == pytest.approx(10.1) and rows[0]["delta"] is None and rows[0]["expiration"] == "2026-12-18"
    assert any("delayed" in w for w in res.warnings)
    assert ("chain", ["SPY"], "2026-12-18", False) in seen and ("chain", ["SPY"], "2026-12-18", True) in seen
    # without an expiration the library cannot answer a window; a library with nothing says so
    assert_empty(chain_module.option_chain("SPY"), "one expiration")
    monkeypatch.setattr(_lib.ft_options, "get_option_expiry_dates", lambda symbol: [])
    assert_empty(chain_module.expiry_dates("SPY"), "no expirations")
    monkeypatch.setattr(_lib.ft_options, "get_option_chains", lambda tickers, expiration_date, put_option=False: pd.DataFrame())
    assert_empty(chain_module.option_chain("SPY", expiration="2026-12-18"), "no contracts")


def test_a_failure_of_a_connected_chain_is_never_replaced_by_delayed_data_FT_033(monkeypatch):
    def broken(*args, **kwargs):
        raise market_options.OptionsError("schwab.auth", 401, "session ended")

    called = []
    monkeypatch.setattr(market_options, "expirations", broken)
    monkeypatch.setattr(market_options, "options_chain", broken)
    monkeypatch.setattr(_lib.ft_options, "get_option_expiry_dates", lambda s: called.append(s) or ["2026-12-18"])
    monkeypatch.setattr(_lib.ft_options, "get_option_chains", lambda *a, **k: called.append(a) or pd.DataFrame())
    assert_empty(chain_module.expiry_dates("SPY"), "schwab.auth")
    assert_empty(chain_module.option_chain("SPY", expiration="2026-12-18"), "schwab.auth")
    assert called == [], "the library source must not answer when a connected chain failed"


def test_chain_inputs_are_checked_FT_033(connected):
    assert_empty(chain_module.option_chain("SPY", kind="straddle"), "kind")
    for bad in ("", "S P Y", "SPY;DROP", "A" * 13, "SPY/../x"):
        with pytest.raises(BadInput):
            chain_module.expiry_dates(bad)
    with pytest.raises(BadInput):
        chain_module.expiry_dates(123)
    with pytest.raises(BadInput):
        chain_module.option_chain("SPY", expiration=20261218)
    with pytest.raises(BadInput):
        chain_module.option_chain("SPY", limit=0)
    with pytest.raises(BadInput):
        chain_module.option_chain("SPY", desk_id="3")
    with pytest.raises(BadInput):
        chain_module.option_chain("SPY", min_days=-1)
    assert connected["chain"] == [] and connected["expirations"] == [], "a bad input never reaches the market module"
    assert chain_module.expiry_dates(" brk.b ").params["symbol"] == "BRK.B"
