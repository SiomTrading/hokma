"""Upgrade from Hokma 1.0.8 to 1.0.9 (SPEC-015 4.4, test 10.1): the connections and the saved settings of 1.0.8 read the
same in 1.0.9. Every provider the owner had is listed as added with the same switch, the order of `equity` is the same,
`bars` starts as a copy of it, and the settings file stays at version 2 and is not rewritten by reading it."""
import json

import pytest

from hokma.connections import runtime as connections_runtime
from hokma.connections.store import SETTINGS_VERSION, ConnectionSettings
from hokma.core.config import hokma_data_dir
from live_fixtures import AUTH, live  # noqa: F401  (fixture)
from test_live_market import FINNHUB_KEY, KEY_ID, SECRET

DEFAULT_BARS = ["schwab", "alpaca", "finnhub"]
FILE_108 = {"version": 2,
            "enabled": {"alpaca": True, "finnhub": False, "siomtrading_futures": True, "coinbase": True},
            "priority": {"equity": ["finnhub", "alpaca"], "crypto": ["finnhub", "coinbase"]}}
FUTURES_KEY = "futures-key-value-1234"


def write_108(live, body: dict = FILE_108) -> bytes:
    """The state of a 1.0.8 install: the file and the keys in the system store."""
    path = hokma_data_dir() / "connections.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(body, indent=2)
    path.write_text(text, encoding="utf-8")
    live.keyring.items[("hokma-market", "alpaca")] = json.dumps({"key_id": KEY_ID, "secret": SECRET})
    live.keyring.items[("hokma-market", "finnhub")] = FINNHUB_KEY
    live.keyring.items[("hokma-market", "siomtrading_futures")] = FUTURES_KEY
    return path.read_bytes()


def listing(client) -> dict:
    return client.get("/api/connections", headers=AUTH).json()


def test_the_providers_of_108_are_added_with_the_same_switch(live):
    write_108(live)
    with live.app() as client:
        cards = {c["id"]: c for c in listing(client)["connections"]}
        for provider_id, enabled in (("alpaca", True), ("finnhub", False), ("siomtrading_futures", True),
                                     ("coinbase", True)):
            assert cards[provider_id]["added"] is True, provider_id
            assert cards[provider_id]["enabled"] is enabled, provider_id
        assert cards["finnhub"]["key_state"] == "saved"               # paused: key saved, switched off, still listed
        assert cards["coinbase"]["key_state"] == "not_required"
        assert cards["schwab"]["added"] is False and cards["schwab"]["key_state"] == "missing"


def test_equity_is_the_same_and_bars_is_a_copy_of_it(live):
    write_108(live)
    with live.app() as client:
        body = listing(client)
        assert body["uses"]["equity"] == ["finnhub", "alpaca", "schwab"]       # the saved order, the new provider last
        assert body["uses"]["bars"] == body["uses"]["equity"]
        assert body["groups"]["equity"] == body["uses"]["equity"]
        assert body["groups"]["crypto"] == ["finnhub", "coinbase"]
        assert "bars" not in body["groups"]                                    # groups keeps its 1.0.8 keys
        assert body["uses"]["options"] == ["schwab", "alpaca"] and body["uses"]["news"] == ["finnhub", "alpaca"]
        assert body["uses"]["future"] == ["siomtrading_futures"]


def test_reading_does_not_rewrite_the_file_and_it_stays_in_version_2(live):
    before = write_108(live)
    path = hokma_data_dir() / "connections.json"
    stamp = path.stat().st_mtime_ns
    with live.app() as client:
        for _ in range(3):
            listing(client)
        assert path.read_bytes() == before and path.stat().st_mtime_ns == stamp
    assert json.loads(before)["version"] == SETTINGS_VERSION == 2
    settings = ConnectionSettings(path)
    assert settings.order("bars", ("schwab", "alpaca", "finnhub")) == ["finnhub", "alpaca", "schwab"]
    assert path.read_bytes() == before                                          # not even loading it writes


def test_the_first_change_saves_the_copy_and_still_writes_version_2(live):
    write_108(live)
    with live.app() as client:
        assert client.put("/api/connections/priority", headers=AUTH,
                          json={"asset_class": "future", "order": ["siomtrading_futures"]}).status_code == 200
    saved = json.loads((hokma_data_dir() / "connections.json").read_text())
    assert saved["version"] == 2
    assert saved["priority"]["equity"] == ["finnhub", "alpaca"] and saved["priority"]["bars"] == ["finnhub", "alpaca"]
    with live.app() as again:
        assert listing(again)["uses"]["bars"] == ["finnhub", "alpaca", "schwab"]


def test_a_file_that_already_has_bars_is_not_overwritten_by_equity(live):
    body = {**FILE_108, "priority": {"equity": ["finnhub", "alpaca"], "bars": ["alpaca", "finnhub", "schwab"]}}
    write_108(live, body)
    with live.app() as client:
        uses = listing(client)["uses"]
        assert uses["equity"] == ["finnhub", "alpaca", "schwab"] and uses["bars"] == ["alpaca", "finnhub", "schwab"]


def test_without_a_saved_equity_order_bars_takes_the_catalog_default(live):
    write_108(live, {"version": 2, "enabled": {"alpaca": True}, "priority": {}})
    with live.app() as client:
        uses = listing(client)["uses"]
        assert uses["equity"] == DEFAULT_BARS and uses["bars"] == DEFAULT_BARS


def test_adding_schwab_to_an_install_of_108_moves_only_schwab(live):
    write_108(live)
    with live.app() as client:
        listing(client)
        connections_runtime.get_service().mark_added("schwab")
        uses = listing(client)["uses"]
        assert uses["equity"] == ["schwab", "finnhub", "alpaca"] and uses["bars"] == ["schwab", "finnhub", "alpaca"]
        assert uses["options"] == ["schwab", "alpaca"]
        assert uses["crypto"] == ["finnhub", "coinbase"] and uses["news"] == ["finnhub", "alpaca"]
        cards = {c["id"]: c for c in listing(client)["connections"]}
        assert cards["finnhub"]["enabled"] is False and cards["alpaca"]["enabled"] is True   # nobody else changed
