"""Hokma 1.0.8: Databento and Massive are retired. Once, the first start removes their saved keys and their traces in
connections.json (version 1 becomes 2). A second start does nothing, and a key value is never logged."""
import json
import logging

from hokma.connections.service import ConnectionService
from hokma.connections.store import SERVICE, ConnectionSettings, KeyStore
from hokma.core.config import hokma_data_dir
from hokma.market.providers import get_catalog
from live_fixtures import AUTH, live  # noqa: F401  (fixture)

SENTINEL = "SENTINEL-retired-key-" + "9" * 12
OLD_FILE = {
    "version": 1,
    "enabled": {"databento": True, "massive": True, "alpaca": True, "coinbase": False},
    "priority": {"future": ["massive", "databento", "siomtrading_futures"],
                 "equity": ["databento", "alpaca", "finnhub"]},
}


def settings_path():
    return hokma_data_dir() / "connections.json"


def write_old_file(file=None) -> None:
    settings_path().parent.mkdir(parents=True, exist_ok=True)
    settings_path().write_text(json.dumps(OLD_FILE if file is None else file), encoding="utf-8")


def stored(live_state) -> set[str]:
    return {name for (service, name) in live_state.keyring.items if service == SERVICE}


def put_keys(live_state, *names: str) -> None:
    for name in names:
        live_state.keyring.items[(SERVICE, name)] = SENTINEL if name in ("databento", "massive") else "other-key-" + name


def new_service() -> ConnectionService:
    return ConnectionService(get_catalog(), KeyStore(), ConnectionSettings())


def test_the_first_start_removes_the_retired_keys_and_cleans_the_file(live):
    put_keys(live, "databento", "massive", "alpaca", "finnhub")
    write_old_file()
    service = new_service()
    service.load()
    assert stored(live) == {"alpaca", "finnhub"}
    saved = json.loads(settings_path().read_text())
    assert saved["version"] == 2
    assert "databento" not in saved["enabled"] and "massive" not in saved["enabled"]
    assert saved["enabled"] == {"alpaca": True, "coinbase": False}
    assert all("databento" not in order and "massive" not in order for order in saved["priority"].values())
    assert saved["priority"] == {"future": ["siomtrading_futures"], "equity": ["alpaca", "finnhub"]}
    assert service.order("future") == ["siomtrading_futures"]
    assert service.has_key("alpaca") and service.has_key("finnhub")


def test_a_second_start_does_not_touch_the_store_again(live, monkeypatch):
    put_keys(live, "databento", "massive", "alpaca")
    write_old_file()
    new_service().load()
    calls: list[str] = []
    original = KeyStore.delete

    def spy(self, provider_id):
        calls.append(provider_id)
        return original(self, provider_id)
    monkeypatch.setattr(KeyStore, "delete", spy)
    before = settings_path().read_text()
    again = new_service()
    again.load()
    assert calls == [] and settings_path().read_text() == before
    assert stored(live) == {"alpaca"}


def test_the_first_start_deletes_through_the_store_once_per_retired_provider(live, monkeypatch):
    put_keys(live, "databento", "massive")
    write_old_file()
    calls: list[str] = []
    original = KeyStore.delete

    def spy(self, provider_id):
        calls.append(provider_id)
        return original(self, provider_id)
    monkeypatch.setattr(KeyStore, "delete", spy)
    new_service().load()
    assert sorted(calls) == ["databento", "massive"]


def test_with_the_store_unavailable_it_starts_and_the_file_is_still_version_2(live, monkeypatch):
    put_keys(live, "databento", "massive", "alpaca")
    write_old_file()
    monkeypatch.setattr(KeyStore, "available", lambda self: False)
    service = new_service()
    service.load()                                                      # no error
    saved = json.loads(settings_path().read_text())
    assert saved["version"] == 2 and "databento" not in saved["enabled"] and "massive" not in saved["enabled"]
    assert service.order("future") == ["siomtrading_futures"]


def test_without_a_settings_file_the_orphan_keys_are_removed_and_the_file_is_created(live):
    put_keys(live, "databento", "massive", "finnhub")
    assert not settings_path().exists()
    new_service().load()
    assert stored(live) == {"finnhub"}
    saved = json.loads(settings_path().read_text())
    assert saved["version"] == 2 and saved["enabled"] == {} and saved["priority"] == {}


def test_the_log_names_the_provider_and_never_the_key(live, caplog):
    caplog.set_level(logging.DEBUG)
    put_keys(live, "databento", "massive")
    write_old_file()
    new_service().load()
    assert "retired provider removed: databento" in caplog.text
    assert "retired provider removed: massive" in caplog.text
    assert SENTINEL not in caplog.text and SENTINEL[:12] not in caplog.text


def test_a_retired_provider_is_not_in_the_api_anymore(live):
    with live.app() as client:
        response = client.put("/api/connections/databento/key", json={"key": SENTINEL}, headers=AUTH)
        assert response.status_code == 404 and response.json()["detail"]["code"] == "connections.unknown_provider"
        assert SENTINEL not in response.text
        ids = [c["id"] for c in client.get("/api/connections", headers=AUTH).json()["connections"]]
        assert "databento" not in ids and "massive" not in ids


def test_an_unreadable_settings_file_is_not_migrated_nor_rewritten(live, caplog):
    put_keys(live, "databento", "massive", "alpaca")
    settings_path().parent.mkdir(parents=True, exist_ok=True)
    settings_path().write_text("{ not json", encoding="utf-8")
    caplog.set_level(logging.ERROR)
    service = new_service()
    service.load()
    assert settings_path().read_text(encoding="utf-8") == "{ not json"
    assert service.settings.retire(("databento", "massive")) == []
    assert settings_path().read_text(encoding="utf-8") == "{ not json"
    assert stored(live) == {"databento", "massive", "alpaca"}
    assert "could not be read" in caplog.text
