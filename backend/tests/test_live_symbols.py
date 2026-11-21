"""The symbol map, the provider catalog file and the settings file."""
import json
from datetime import date

import pytest

from hokma.connections.store import ConnectionSettings
from hokma.market.providers import CATALOG_FILE, get_catalog, parse_catalog, set_catalog
from hokma.market.symbols import classify, to_provider


@pytest.mark.parametrize("symbol,kind,expected", [
    ("NQ=F", None, "future"), ("GC=F", "future", "future"), ("EURUSD=X", None, "forex"), ("BTC-USD", None, "crypto"),
    ("ETH-USDT", "crypto", "crypto"), ("AAPL", None, "equity"), ("BRK-B", "equity", "equity"), ("SPY", "etf", "equity"),
    ("^GSPC", "index", None), ("^GSPC", None, None), ("AAPL", "index", None), ("TOOLONGNAME", None, None)])
def test_the_asset_class_of_a_symbol(symbol, kind, expected):
    assert classify(symbol, kind) == expected


@pytest.mark.parametrize("provider,symbol,asset,expected", [
    ("coinbase", "BTC-USD", "crypto", "BTC-USD"), ("coinbase", "AAPL", "equity", None),
    ("alpaca", "AAPL", "equity", "AAPL"), ("alpaca", "BRK-B", "equity", "BRK.B"), ("alpaca", "BTC-USD", "crypto", None),
    ("finnhub", "AAPL", "equity", "AAPL"), ("finnhub", "EURUSD=X", "forex", "OANDA:EUR_USD"),
    ("finnhub", "BTC-USD", "crypto", "BINANCE:BTCUSDT"), ("finnhub", "ETH-USDT", "crypto", "BINANCE:ETHUSDT"),
    ("finnhub", "NQ=F", "future", None),
    ("siomtrading_futures", "ES=F", "future", "ES"), ("siomtrading_futures", "AAPL", "equity", None),
    ("coinbase", "^GSPC", None, None)])
def test_the_symbol_each_provider_expects(provider, symbol, asset, expected):
    assert to_provider(provider, symbol, asset, date(2026, 11, 5)) == expected


def test_the_shipped_catalog_has_the_four_providers_in_the_default_order():
    catalog = get_catalog()
    assert list(catalog.providers) == ["coinbase", "alpaca", "finnhub", "siomtrading_futures"]
    assert catalog.default_priority == {"equity": ("alpaca", "finnhub"), "crypto": ("coinbase", "finnhub"),
                                        "forex": ("finnhub",), "future": ("siomtrading_futures",)}
    assert catalog.providers["finnhub"].data_state == "live" and catalog.providers["finnhub"].max_symbols == 50
    assert catalog.providers["alpaca"].max_symbols == 30 and catalog.providers["coinbase"].needs_key is False
    assert catalog.providers["siomtrading_futures"].url.startswith("wss://")
    assert not any(hasattr(p, "premium") for p in catalog.providers.values())


def test_a_provider_missing_from_the_default_order_goes_last_and_a_bad_file_is_refused():
    raw = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
    raw["default_priority"]["equity"] = ["finnhub"]
    assert parse_catalog(raw).default_priority["equity"][0] == "finnhub"
    assert set(parse_catalog(raw).default_priority["equity"]) == {"finnhub", "alpaca"}
    raw["providers"]["finnhub"]["data_state"] = "maybe"
    with pytest.raises(ValueError):
        parse_catalog(raw)
    raw["providers"]["finnhub"]["data_state"] = "live"
    raw["providers"]["finnhub"]["asset_classes"] = ["bonds"]
    with pytest.raises(ValueError):
        parse_catalog(raw)


def test_the_catalog_can_be_swapped_and_restored():
    original = get_catalog()
    raw = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
    raw["providers"]["finnhub"]["data_state"] = "live"
    set_catalog(parse_catalog(raw))
    assert get_catalog().providers["finnhub"].data_state == "live"
    set_catalog(None)
    assert get_catalog().providers["finnhub"].data_state == original.providers["finnhub"].data_state


def test_the_settings_file_survives_garbage_and_never_keeps_anything_but_choices(tmp_path):
    path = tmp_path / "connections.json"
    path.write_text("{ not json", encoding="utf-8")
    settings = ConnectionSettings(path)
    assert settings.enabled("coinbase", True) is True and settings.order("future", ("a", "b")) == ["a", "b"]
    path.write_text(json.dumps({"enabled": {"coinbase": "yes", "alpaca": False}, "priority": {"future": ["b", 3, "x"]},
                                "key": "should-not-matter"}), encoding="utf-8")
    settings = ConnectionSettings(path)
    assert settings.enabled("coinbase", True) is True and settings.enabled("alpaca", True) is False
    assert settings.order("future", ("a", "b")) == ["b", "a"]
    settings.set_enabled("alpaca", True)
    assert set(json.loads(path.read_text())) == {"version", "enabled", "priority"}
