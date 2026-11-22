"""SPEC-016 T1 tool: the measuring run records from the Streamer session of the HUB (never a second connection), saves the raw answers, answers the open
points, keeps only the permissions of the user preferences, and writes NOTHING if a token, an id, an account number or a hash would go in the file."""
import asyncio
import json
from pathlib import Path

import pytest

from hokma.market.adapters import SchwabSource
from hokma.market.providers import get_catalog
from hokma.schwab import diagnostic, stream
from schwab_support import FAKE_TOKEN, FakeSchwab
from streamer_fake_server import CORREL, CUSTOMER, StreamerFakeServer
from test_schwab_stream import BACKOFF, FAST, preference

OPTION = "QQQ   261127C00500000"
ACCOUNT, HASH = "12345678", "A1B2C3D4E5F60718293A4B5C6D7E8F90"


class Hub:
    """What the run needs of the hub: the one source of Schwab it already has."""

    def __init__(self, source) -> None:
        self.source, self.asked = source, 0

    async def provider_source(self, provider_id):
        self.asked += 1
        return self.source


@pytest.fixture
def server():
    fake = StreamerFakeServer().start()
    fake.unavailable.update({"LEVELONE_FUTURES", "CHART_FUTURES"})
    stream.allow_local_for_tests(True)
    yield fake
    stream.allow_local_for_tests(False)
    fake.stop()


def rest_for(server, leak: bool = False) -> FakeSchwab:
    rest = FakeSchwab()
    body = preference(server.url)
    rest.on("GET", "/trader/v1/userPreference", body)
    rest.on("GET", "/trader/v1/accounts/accountNumbers", [{"accountNumber": ACCOUNT, "hashValue": HASH}])
    candles = [{"datetime": 1_700_000_000_000, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10}]
    if leak:
        candles[0]["volume"] = int(ACCOUNT)                     # an account number turning up in a data field: the check must see it
    rest.on("GET", "/marketdata/v1/pricehistory", {"symbol": "QQQ", "empty": False, "candles": candles})
    rest.on("GET", "/marketdata/v1/quotes", {"QQQ": {"symbol": "QQQ", "assetMainType": "EQUITY", "realtime": True, "quote": {"lastPrice": 500.0}}})
    session = {"regularMarket": [{"start": "2026-11-20T09:30:00-05:00", "end": "2026-11-20T16:00:00-05:00"}]}
    for market in ("equity", "option", "future"):
        rest.on("GET", f"/marketdata/v1/markets/{market}", {market.upper(): {"isOpen": True, "sessionHours": session}})
    rest.on("GET", "/marketdata/v1/expirationchain", {"expirationList": [{"expirationDate": "2999-01-01", "daysToExpiration": 1}]})
    rest.on("GET", "/marketdata/v1/chains", {"symbol": "QQQ", "underlyingPrice": 500.1, "callExpDateMap": {"2999-01-01:1": {
        "500.0": [{"symbol": OPTION, "bid": 1.0, "ask": 1.1, "mark": 1.05, "last": 1.0}]}}, "putExpDateMap": {}})
    return rest


async def drive(server, hub, rest, seconds=1.5, **kwargs):
    task = asyncio.create_task(diagnostic.run("QQQ", seconds, hub=hub, **kwargs))
    for _ in range(300):
        if "QQQ" in server.subscribed.get("CHART_EQUITY", set()) and server.subscribed.get("LEVELONE_OPTIONS"):
            break
        await asyncio.sleep(0.02)
    server.heartbeat()
    server.data("CHART_EQUITY", [{"key": "QQQ", "1": 10.0, "2": 10.5, "3": 9.9, "4": 10.2, "5": 100, "6": 1, "7": 1_700_000_040_000}])
    server.data("CHART_EQUITY", [{"key": "QQQ", "1": 10.0, "2": 10.6, "3": 9.9, "4": 10.4, "5": 150, "6": 1, "7": 1_700_000_040_000}])
    server.data("LEVELONE_OPTIONS", [{"key": OPTION, "2": 1.0, "3": 1.1, "37": 1.05}])
    return await task


def test_it_records_from_the_session_of_the_hub_without_opening_a_second_streamer(server, tmp_path, monkeypatch):
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(diagnostic, "MIN_SECONDS", 1.0)

    async def go():
        rest = rest_for(server)
        rest.__enter__()
        source = SchwabSource(get_catalog().get("schwab"), backoff=BACKOFF, config=FAST)
        try:
            await source.connect()
            await source.subscribe(["SPY"])                       # a chart of the hub: it must still be followed after the run
            for _ in range(200):
                if server.subscribed.get("LEVELONE_EQUITIES") == {"SPY"}:
                    break
                await asyncio.sleep(0.02)
            result = await drive(server, Hub(source), rest)
            for _ in range(100):
                if server.subscribed.get("LEVELONE_EQUITIES") == {"SPY"}:
                    break
                await asyncio.sleep(0.02)
            return result, dict(server.subscribed)
        finally:
            await source.close()
            rest.__exit__()
    result, after = asyncio.run(go())
    assert server.attempts == 1 and server.log.count("ADMIN LOGIN") == 1                     # ONE connection, the one of the hub
    assert after["LEVELONE_EQUITIES"] == {"SPY"} and not after.get("LEVELONE_OPTIONS")      # it took away what it added, and only that
    summary = result["summary"]
    assert summary["chart_equity"]["mode"] == "in_progress" and OPTION in summary["option_keys_answered"]
    assert "LEVELONE_FUTURES ADD code=11" in summary["answers_seen"]
    text = Path(result["file"]).read_text(encoding="utf-8")
    assert FAKE_TOKEN not in text and CUSTOMER not in text and CORREL not in text


def test_only_the_permissions_of_the_preferences_are_kept_never_the_accounts(server, tmp_path, monkeypatch):
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(diagnostic, "MIN_SECONDS", 1.0)

    async def go():
        rest = rest_for(server)
        rest.__enter__()
        source = SchwabSource(get_catalog().get("schwab"), backoff=BACKOFF, config=FAST)
        try:
            await source.connect()
            return await drive(server, Hub(source), rest)
        finally:
            await source.close()
            rest.__exit__()
    document = json.loads(Path(asyncio.run(go())["file"]).read_text(encoding="utf-8"))
    assert document["rest"]["offers"] == [{"level2Permissions": True, "mktDataPermission": "NP"}]
    text = json.dumps(document)
    for forbidden in (ACCOUNT, HASH, "accountNumber", "hashValue", "streamerInfo", "displayAcctId", "ACCT-DISPLAY"):
        assert forbidden not in text, forbidden


def test_a_missing_offers_block_is_an_empty_object_and_not_the_whole_answer():
    rest = FakeSchwab()
    rest.on("GET", "/trader/v1/userPreference", [{"accounts": [{"accountNumber": ACCOUNT}], "streamerInfo": [{"schwabClientCorrelId": "x"}]}])
    rest.on("GET", "/marketdata/v1/pricehistory", (404, {}))
    rest.__enter__()
    try:
        import datetime as dt

        out = diagnostic.collect_rest("QQQ", dt.date(2026, 11, 20))
    finally:
        rest.__exit__()
    assert out["offers"] == {} and ACCOUNT not in json.dumps(out)


@pytest.mark.parametrize("text,secrets,expected", [
    ('{"x": "plain"}', ["12345678"], None),
    ('{"volume": 12345678}', ["12345678"], "a secret value"),
    ('{"hash": "A1B2C3D4E5F60718293A4B5C6D7E8F90"}', ["A1B2C3D4E5F60718293A4B5C6D7E8F90"], "a secret value"),
    ('{"accounts": []}', [], "the field accounts"),
    ('{"accountNumber": "1"}', [], "the field accountNumber"),
    ('{"hashValue": "1"}', [], "the field hashValue"),
    ('{"streamerInfo": []}', [], "the field streamerInfo"),
])
def test_the_file_check_sees_values_and_field_names_of_accounts_and_the_session(text, secrets, expected):
    assert diagnostic.leaks(text, secrets) == expected


def test_a_leak_writes_no_file(server, tmp_path, monkeypatch):
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(diagnostic, "MIN_SECONDS", 1.0)

    async def go():
        rest = rest_for(server, leak=True)
        rest.__enter__()
        source = SchwabSource(get_catalog().get("schwab"), backoff=BACKOFF, config=FAST)
        try:
            await source.connect()
            return await drive(server, Hub(source), rest)
        finally:
            await source.close()
            rest.__exit__()
    with pytest.raises(RuntimeError, match="nothing was written"):
        asyncio.run(go())
    assert not (tmp_path / "diagnostics").exists() or not list((tmp_path / "diagnostics").iterdir())


def test_two_runs_at_once_are_refused_and_the_flag_is_released(server, tmp_path, monkeypatch):
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(diagnostic, "MIN_SECONDS", 1.0)

    async def go():
        rest = rest_for(server)
        rest.__enter__()
        source = SchwabSource(get_catalog().get("schwab"), backoff=BACKOFF, config=FAST)
        try:
            await source.connect()
            first = asyncio.create_task(diagnostic.run("QQQ", 1.2, hub=Hub(source)))
            while not diagnostic._running:                  # the first run holds the line: no guess at how long that takes
                await asyncio.sleep(0.005)
            with pytest.raises(diagnostic.DiagnosticBusy):
                await diagnostic.run("QQQ", 1.2, hub=Hub(source))
            await first
            await diagnostic.run("QQQ", 1.0, hub=Hub(source))      # released: a new run is allowed
        finally:
            await source.close()
            rest.__exit__()
    asyncio.run(go())


def test_without_a_connected_schwab_the_run_says_so_and_opens_nothing():
    from hokma.schwab.http import SchwabError

    class Empty:
        async def provider_source(self, provider_id):
            return None

    with pytest.raises(SchwabError):
        asyncio.run(diagnostic.run("QQQ", 5, hub=Empty()))


def test_sanitize_drops_the_ids_and_the_server_names_and_keeps_the_market_data():
    message = {"response": [{"service": "ADMIN", "command": "LOGIN", "requestid": "1", "SchwabClientCorrelId": "secret-correl",
                             "content": {"code": 0, "msg": "server=s0166bdv-1;status=NP"}}],
               "data": [{"service": "LEVELONE_EQUITIES", "content": [{"key": "QQQ", "1": 500.1}]}]}
    clean = stream.sanitize(message)
    assert "secret-correl" not in json.dumps(clean) and "s0166bdv" not in json.dumps(clean)
    assert clean["response"][0]["content"] == {"code": 0, "msg": "status=NP"} and clean["data"][0]["content"] == [{"key": "QQQ", "1": 500.1}]


def test_the_front_contract_is_a_quarter_code_with_the_two_digit_year():
    import datetime as dt

    assert diagnostic.front_contract("NQ", dt.date(2026, 11, 21)) == "/NQZ26"
