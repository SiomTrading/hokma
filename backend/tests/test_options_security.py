"""Security of the option chain (SPEC-014): the key never leaves the headers, the real host is read with one GET and for
agents it stays locked."""
import json
import logging
import urllib.parse
from pathlib import Path

import pytest
from hardblock_support import box  # noqa: F401  (fixture)
from options_support import DATA, DEMO_KEY, LIVE, PAPER, REAL_KEY, install

import hokma
from hokma.kit import hardblock
from hokma.market import options

ROOT = Path(hokma.__file__).parent
EXCEPTION = {"host": "api.alpaca.markets", "method": "GET", "path": "/v2/options/contracts"}


def secrets_of(key):
    return [key["key_id"], key["secret"], json.dumps(key, separators=(",", ":"))]


@pytest.mark.parametrize("key", [DEMO_KEY, REAL_KEY])
def test_the_key_is_not_in_any_answer_url_or_log_and_goes_only_in_the_headers(monkeypatch, caplog, key):
    fake = install(monkeypatch, key)
    caplog.set_level(logging.DEBUG)
    out = options.options_chain("AAPL", limit=5)
    group = options.chain("AAPL", "2026-12-18", 6)
    bar = options.underlying("AAPL")
    listing = options.expirations("AAPL")
    everything = json.dumps([out, group, bar, listing]) + caplog.text
    for secret in secrets_of(key):
        assert secret not in everything
        assert all(secret not in url for url, _ in fake.calls)
    assert fake.calls and all(headers == {"APCA-API-KEY-ID": key["key_id"], "APCA-API-SECRET-KEY": key["secret"]}
                              for _, headers in fake.calls)


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_the_key_is_not_in_an_error_either(monkeypatch, status):
    fake = install(monkeypatch, DEMO_KEY)
    fake.status[PAPER] = status
    with pytest.raises(options.OptionsError) as error:
        options.options_chain("AAPL")
    assert not any(secret in str(error.value) for secret in secrets_of(DEMO_KEY))


def test_a_value_that_comes_back_from_alpaca_with_the_key_inside_is_scrubbed(monkeypatch):
    fake = install(monkeypatch, DEMO_KEY)
    fake.indicative = {s: {**v, "latestQuote": {**v["latestQuote"], "t": "echo " + DEMO_KEY["secret"]}}
                       for s, v in fake.indicative.items()}
    out = options.options_chain("AAPL", limit=1)
    assert DEMO_KEY["secret"] not in json.dumps(out) and "[key]" in out["contracts"][0]["quote_time"]


@pytest.mark.parametrize("key", [DEMO_KEY, REAL_KEY])
def test_only_the_three_known_hosts_are_called_and_the_real_one_only_for_the_contracts_list(monkeypatch, key):
    fake = install(monkeypatch, key)
    options.options_chain("AAPL")
    options.chain("AAPL", "2026-12-18,2027-01-15")
    options.expirations("AAPL")
    options.underlying("AAPL")
    assert set(fake.hosts()) <= {PAPER, DATA, LIVE}
    for url, _ in fake.calls:
        parts = urllib.parse.urlsplit(url)
        assert parts.scheme == "https"
        if parts.netloc == LIVE:
            assert parts.path == "/v2/options/contracts" and not parts.fragment
        if parts.netloc == PAPER:
            assert parts.path == "/v2/options/contracts"
    if key is DEMO_KEY:
        assert LIVE not in fake.hosts()


def test_the_module_has_no_way_to_name_orders_positions_or_the_account():
    text = (ROOT / "market" / "options.py").read_text(encoding="utf-8")
    for forbidden in ("/v2/orders", "/v2/positions", "/v2/account", "method=", "urlopen", "POST", "DELETE", "PATCH"):
        assert forbidden not in text.replace("DELETE FROM", "")


def test_options_py_is_the_only_python_file_that_names_the_real_host():
    named = {str(p.relative_to(ROOT)) for p in ROOT.rglob("*.py")
             if "api.alpaca.markets" in p.read_text(encoding="utf-8").replace("paper-api.alpaca.markets", "")}
    assert named == {str(Path("market") / "options.py")}


def test_the_real_host_is_not_touched_when_the_lock_has_no_exception_for_it(monkeypatch):
    fake = install(monkeypatch, REAL_KEY)
    brokers = hardblock.load_brokers()
    brokers["read_exceptions"] = []
    monkeypatch.setattr(hardblock, "load_brokers", lambda path=hardblock.BROKERS_FILE: brokers)
    with pytest.raises(options.OptionsError):
        options.options_chain("AAPL")
    assert LIVE not in fake.hosts()
    brokers["read_exceptions"] = [{**EXCEPTION, "match": "exact_path_query_allowed", "consumer": "someone_else"}]
    with pytest.raises(options.OptionsError):
        options.options_chain("AAPL")
    assert LIVE not in fake.hosts()


# ------------------------------------------------------------------------------------------ the lock

def allows(**over):
    rule = {**EXCEPTION, "consumer": "hokma_backend", **over}
    return hardblock.read_exception_allows(rule["host"], rule["method"], rule["path"], rule["consumer"])


def test_the_shipped_exception_is_one_get_on_one_path_for_one_consumer():
    rules = hardblock.load_brokers()["read_exceptions"]
    assert [r for r in rules if r["host"] == "api.alpaca.markets"] == [
        {"host": "api.alpaca.markets", "method": "GET", "path": "/v3/calendar/{market}", "match": "path_template", "consumer": "hokma_backend",
         "reason": "Market calendar with pre, core and post of a day, read only (SPEC-016 A3)", "params": {"market": "NYSE|NASDAQ|XNYS|XNAS"}},
        {"host": "api.alpaca.markets", "method": "GET", "path": "/v3/clock", "match": "exact_path_query_allowed", "consumer": "hokma_backend",
         "reason": "Market phase now (closed, pre, core, post), read only (SPEC-016 A3)"},
        {"host": "api.alpaca.markets", "method": "GET", "path": "/v2/options/contracts",
         "match": "exact_path_query_allowed", "consumer": "hokma_backend",
         "reason": "Option contracts and open interest, read only (SPEC-014)"}]       # three reads, all GET, never an order
    assert {r["host"] for r in rules} == {"api.alpaca.markets", "api.schwabapi.com"}      # SPEC-015 added Schwab's reads
    assert "api.alpaca.markets" in hardblock.load_brokers()["hosts"]          # still a locked host


def test_the_exception_allows_exactly_that_request():
    assert allows() and allows(method="get") and allows(path="/v2/options/contracts?underlying_symbols=AAPL&limit=1000")


@pytest.mark.parametrize("over", [{"method": "POST"}, {"method": "PUT"}, {"method": "PATCH"}, {"method": "DELETE"},
                                  {"path": "/v2/orders"}, {"path": "/v2/positions"}, {"path": "/v2/account"},
                                  {"path": "/v2/options/contracts/AAPL261218C00220000"},
                                  {"path": "/v2/options/contracts/"}, {"path": "/v2/options/contractsX"},
                                  {"path": "/V2/options/contracts"}, {"host": "api.tradier.com"},
                                  {"host": "api.alpaca.markets.evil.com"}, {"consumer": "agent"}, {"consumer": ""}])
def test_anything_else_stays_blocked(over):
    assert not allows(**over)


def test_a_rule_that_is_not_an_exact_path_match_does_not_count():
    brokers = {"read_exceptions": [{**EXCEPTION, "consumer": "hokma_backend", "match": "prefix"}]}
    assert not hardblock.read_exception_allows("api.alpaca.markets", "GET", "/v2/options/contracts", "hokma_backend", brokers)
    assert not hardblock.read_exception_allows("api.alpaca.markets", "GET", "/v2/options/contracts", "hokma_backend", {})


@pytest.mark.parametrize("url", ["https://api.alpaca.markets/v2/options/contracts",
                                 "https://api.alpaca.markets/v2/options/contracts?underlying_symbols=AAPL&limit=1000",
                                 "https://api.alpaca.markets/v2/orders", "https://api.alpaca.markets/v2/positions",
                                 "https://api.alpaca.markets/v2/account"])
def test_for_an_agent_the_real_host_stays_locked_even_for_the_contracts_list(box, url):
    assert box.hit("WebFetch", url=url) == "real_money"
    assert box.bash(f"curl -s {url}") == "real_money"
    assert box.bash(f"curl -s -X GET '{url}' -H 'APCA-API-KEY-ID: x'") == "real_money"
    assert box.bash(f"python3 -c \"import urllib.request as u; u.urlopen('{url}')\"") == "real_money"
    assert box.ps(f"Invoke-RestMethod {url}") == "real_money"


def test_the_hook_as_a_process_refuses_the_contracts_list_for_an_agent(box):
    done = box.run_hook({"tool_name": "WebFetch", "tool_input": {"url": "https://api.alpaca.markets/v2/options/contracts"},
                         "cwd": str(box.work)})
    assert done.returncode == 2 and "HOKMA_BLOCKED real_money" in done.stderr
    ok = box.run_hook({"tool_name": "WebFetch", "tool_input": {"url": "https://paper-api.alpaca.markets/v2/options/contracts"},
                       "cwd": str(box.work)})
    assert ok.returncode == 0                                   # the demo host was never locked
