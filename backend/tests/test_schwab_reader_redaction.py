"""The reason of a Schwab refusal (reader.message_of, B4 of the SPEC-015 round 3): the useful text of errors[] still reaches
the agent, but no account number and no hash of the table (exact, in any case, split by separators, encoded in the URL),
no word of 20 characters or more and no pointer with account segments does. Nothing touches the network."""
import json
import urllib.parse

import pytest

from dbsupport import db, env  # noqa: F401  (fixtures)
from fastapi.testclient import TestClient
from schwab_support import FAKE_HASH, FakeSchwab, clear_fake, load

from hokma.core.app import create_app
from hokma.kit import hokma_tools
from hokma.kit.hokma_tools import Caller
from hokma.rpc.ws import SESSION_TOKEN
from hokma.schwab import accounts, auth, http, reader
from hokma.schwab.http import BAD_REQUEST, NOT_FOUND, UPSTREAM, SchwabError

MARKET = "/marketdata/v1"
AUTH = {"X-Hokma-Token": SESSION_TOKEN}
NUMBER = "12345678"
HASH = FAKE_HASH                                        # A1B2C3D4E5F60718293A4B5C6D7E8F90
SHORT = "90417"                                         # a short number that no shape rule catches
SHORT_HASH = "9f8e7d6c5b4a39281706f5e4d3c2b1a0"
TABLE = load("account_numbers.json") + [{"accountNumber": SHORT, "hashValue": SHORT_HASH}]
HIDDEN = "[hidden]"


@pytest.fixture(scope="module")
def fake_server():
    with FakeSchwab() as fake:
        yield fake


@pytest.fixture
def fake(fake_server, monkeypatch, env):  # noqa: F811
    clear_fake(fake_server)
    http.reset_limits()
    accounts.reset()
    reader.clear_cache()
    monkeypatch.setattr(auth, "is_connected", lambda: True)
    fake_server.on("GET", "/trader/v1/accounts/accountNumbers", TABLE)
    accounts.refresh(force=True)
    yield fake_server
    reader.clear_cache()
    accounts.reset()


def text_of(*entries, code=BAD_REQUEST, status=400) -> str:
    return reader.message_of(SchwabError(code, status, errors=list(entries)))


def leaks(text: str) -> list[str]:
    flat = "".join(ch for ch in urllib.parse.unquote(text).lower() if ch.isalnum())
    return [v for v in (NUMBER, "87654321", SHORT, HASH.lower(), SHORT_HASH) if v in flat]


# ------------------------------------------------------------------------------------------ exact values


@pytest.mark.parametrize("value", [
    NUMBER, "1234-5678", "1234 5678", "1234.5678", "1-2-3-4-5-6-7-8", SHORT, "90 417", "90-417", "90/417",
    HASH, HASH.lower(), "A1B2C3D4-E5F60718-293A4B5C-6D7E8F90", "a1b2 c3d4 e5f6 0718 293a 4b5c 6d7e 8f90",
    "A1B2C3D4.E5F60718.293A4B5C.6D7E8F90", SHORT_HASH, SHORT_HASH.upper(), "9f8e7d6c/5b4a3928/1706f5e4/d3c2b1a0",
    urllib.parse.quote(HASH), urllib.parse.quote(NUMBER, safe="") .replace("1", "%31"), "x" + SHORT + "y"])
@pytest.mark.parametrize("field", ["detail", "title", "id"])
def test_an_account_number_or_hash_never_shows_in_any_field(fake, value, field):
    entry = {"title": "Bad order", "detail": "check the request", "id": "abc", "status": "400"}
    entry[field] = f"the account {value} is wrong"
    text = text_of(entry)
    assert not leaks(text), text
    assert HIDDEN in text


def test_the_pointer_and_the_url_with_an_account_segment_show_no_value(fake):
    for source in ({"pointer": ["/accounts/12345678/orders"]}, {"pointer": f"/accounts/{HASH}/orders"},
                   "/trader/v1/accounts/" + SHORT_HASH + "/orders", "/accounts/55555/orders",
                   {"pointer": "/trader/v1/accounts/" + urllib.parse.quote(HASH) + "/orders"}):
        text = text_of({"title": "Bad", "detail": "see https://api.example.test/trader/v1/accounts/" + HASH + "/orders",
                        "source": source})
        assert not leaks(text) and "55555" not in text, text


def test_source_keeps_only_the_field_name(fake):
    assert text_of({"title": "T", "source": "/accounts/12345678/orders"}).endswith("T (source orders)")
    assert text_of({"title": "T", "source": f"/accounts/{HASH}/orders"}).endswith("T (source orders)")
    assert text_of({"title": "T", "source": {"parameter": "symbol"}}).endswith("T (source symbol)")
    assert text_of({"title": "T", "source": {"header": "Schwab-Client-CorrelId"}}).endswith("(source Schwab-Client-CorrelId)")
    assert text_of({"title": "T", "source": ["fromEnteredTime", "toEnteredTime"]}).endswith("(source fromEnteredTime, toEnteredTime)")
    both = text_of({"title": "T", "source": {"pointer": "/accounts/12345678", "parameter": "status", "extra": {"k": "v"}}})
    assert both.endswith("(source status)") and "pointer" not in both and "extra" not in both and "{" not in both
    assert text_of({"title": "T", "source": {"a": {"b": ["12345678"]}}}).endswith("T")
    assert text_of({"title": "T", "source": [{"parameter": "p"}, 5, None, ["nested"]]}).endswith("(source p)")
    assert text_of({"title": "T", "source": 12345}).endswith("T")


# ------------------------------------------------------------------------------------------ long words


def test_a_word_of_20_or_more_characters_is_hidden_and_19_passes(fake):
    assert HIDDEN in text_of({"title": "token " + "Zq" * 10})
    assert HIDDEN in text_of({"title": "k" * 20})
    assert HIDDEN in text_of({"detail": "value abcdefghijklmnopqrst0"})
    assert HIDDEN not in text_of({"title": "k" * 19})
    assert HIDDEN not in text_of({"title": "Invalid combination of strategy and range"})


def test_an_option_symbol_is_not_hidden(fake):
    for symbol in ("FAKE  261218C00100000", "AAPL  261218C00200000", "SPXW  261218P05000000", "AAPL261218C00100000"):
        text = text_of({"title": "Unknown symbol", "detail": f"{symbol} has no quotes"})
        assert symbol in text and HIDDEN not in text, text


def test_the_useful_text_stays_for_400_404_and_5xx(fake):
    for code, status in ((BAD_REQUEST, 400), (NOT_FOUND, 404), (UPSTREAM, 500)):
        text = text_of({"id": "e1", "status": str(status), "title": "Invalid request", "detail": "strikeCount must be positive",
                        "source": {"parameter": "strikeCount"}}, code=code, status=status)
        assert "Invalid request: strikeCount must be positive (id e1, status " + str(status) + ", source strikeCount)" in text


def test_connection_codes_keep_their_own_text(fake):
    for code, status in (("connections.schwab_denied", 403), ("connections.schwab_expired", 401), ("connections.rate_limited", 429)):
        text = text_of({"title": "x" * 25, "detail": NUMBER}, code=code, status=status)
        assert text == reader.message_of(SchwabError(code, status)) and not leaks(text)


def test_a_cut_field_never_leaves_a_piece_of_a_value(fake):
    text = text_of({"detail": "a " * 148 + HASH})
    assert not leaks(text) and HASH[:12] not in text


# ------------------------------------------------------------------------------------------ real paths out


def echo(value):
    return lambda call: (400, {"errors": [{"id": "e-" + SHORT, "status": "400", "title": "Rejected " + value,
                                           "detail": f"account {value} on /accounts/{HASH}/orders",
                                           "source": {"pointer": [f"/accounts/{NUMBER}/orders"], "parameter": "status"}}]})


@pytest.mark.parametrize("value", ["1234-5678", HASH, "A1B2C3D4-E5F60718-293A4B5C-6D7E8F90", SHORT])
def test_the_tool_answer_and_the_route_answer_show_no_value(db, fake, value):
    fake.on("GET", MARKET + "/expirationchain", echo(value))
    answer, is_error = hokma_tools.call("schwab_expirations", {"symbol": "AAPL"}, Caller({"HOKMA_AGENT": "hokma"}))
    assert is_error and not leaks(answer) and "Rejected" in answer and "source status" in answer
    reader.clear_cache()
    routed = TestClient(create_app()).get("/api/schwab/marketdata/expirationchain", params={"symbol": "AAPL"}, headers=AUTH)
    assert routed.status_code == 400
    body = json.dumps(routed.json())
    assert not leaks(body) and "Rejected" in body and "orders" in body


def test_sensitive_values_is_read_only_internal_and_never_on_a_route_or_tool():
    assert not [n for n in hokma_tools.TOOLS if "sensitive" in n]
    from hokma.schwab.routes import router
    assert not [r for r in router.routes if "sensitive" in getattr(r, "path", "")]


# ------------------------------------------------------------------------------------------ control characters (task 3299, point 3)

CONTROLS = ["%0A", "%0D", "%0D%0A", "%00", "%1B", "%1b%5b31m", "%E2%80%A8", "%E2%80%A9", "%C2%85", "%250A", "%25250D",
            "\n", "\r", "\x1b", "\u2028", "\u2029", "\x85"]


def has_control(text: str) -> bool:
    return any(ord(ch) < 32 or 0x7F <= ord(ch) <= 0x9F or ch in "\u2028\u2029" for ch in text)


@pytest.mark.parametrize("raw", CONTROLS)
@pytest.mark.parametrize("field", ["detail", "title", "id", "source", "pointer", "parameter", "path"])
def test_a_control_character_never_reaches_the_message_even_when_it_is_encoded(fake, raw, field):
    entry = {"title": "Bad order", "detail": "check the request", "id": "abc", "status": "400"}
    if field in ("detail", "title", "id"):
        entry[field] = f"line1{raw}line2"
    elif field == "source":
        entry["source"] = f"/orders{raw}/status"
    elif field == "pointer":
        entry["source"] = {"pointer": [f"/accounts/{raw}/orders{raw}x"]}
    elif field == "parameter":
        entry["source"] = {"parameter": f"sym{raw}bol"}
    else:
        entry["source"] = f"/trader/v1{raw}/orders"
    text = text_of(entry)
    assert not has_control(text), repr(text)
    assert "line1" not in text or "line2" in text                 # the text around it stays


def test_the_tool_answer_and_the_route_answer_have_no_control_character(db, fake):
    fake.on("GET", MARKET + "/expirationchain", lambda call: (400, {"errors": [{
        "title": "Rejected%0Aone%0D%0Atwo%00%1B", "detail": "x%E2%80%A8y%250Az", "source": {"parameter": "sta%0Atus"}}]}))
    answer, is_error = hokma_tools.call("schwab_expirations", {"symbol": "AAPL"}, Caller({"HOKMA_AGENT": "hokma"}))
    assert is_error and "Rejected" in answer and not has_control(answer), repr(answer)
    reader.clear_cache()
    routed = TestClient(create_app()).get("/api/schwab/marketdata/expirationchain", params={"symbol": "AAPL"}, headers=AUTH)
    assert routed.status_code == 400 and "Rejected" in json.dumps(routed.json())
    assert not any(has_control(s) for s in strings_of(routed.json())), routed.json()


def strings_of(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from strings_of(key)
            yield from strings_of(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings_of(item)
