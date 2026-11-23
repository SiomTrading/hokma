"""TA-57 and TA-58: a symbol cannot escape a folder; keys never reach the model or a log."""
import pytest

from hokma.desks.core import safety

SENTINEL_KEY = "SENTINEL-KEY-9f3a77c1d2"
SENTINEL_TOKEN = "SENTINEL-TOKEN-b51e00aa"
SENTINEL_ACCOUNT = "SENTINEL-ACCT-123456789"


@pytest.mark.parametrize("value", ["AAPL", "BRK.B", "^GSPC", "EURUSD=X", "BTC-USD", "NQ=F", "a_b", "ES+1", "X" * 32])
def test_plain_symbols_pass_unchanged_TA_57(value):
    assert safety.safe_ticker_component(value) == value


@pytest.mark.parametrize("value", ["../etc/passwd", "..", ".", "...", "a/b", "a\\b", "AAPL;rm -rf", "A B", "AAPL\n", "$(id)", "a:b", "",
                                   "X" * 33, "AAPL|ls", "a\x00b", "<script>", "%2e%2e"])
def test_dangerous_symbols_are_refused_before_a_provider_TA_57(value):
    with pytest.raises(ValueError):
        safety.safe_ticker_component(value)
    assert safety.is_safe_ticker(value) is False


@pytest.mark.parametrize("value", [None, 5, b"AAPL", ["AAPL"]])
def test_a_symbol_must_be_text_TA_57(value):
    with pytest.raises(ValueError):
        safety.safe_ticker_component(value)


def test_a_custom_limit_TA_57():
    assert safety.safe_ticker_component("ABCDEFGH", max_len=8) == "ABCDEFGH"
    with pytest.raises(ValueError):
        safety.safe_ticker_component("ABCDEFGHI", max_len=8)


def test_a_key_in_a_url_is_removed_TA_58():
    text = f"HTTPSConnectionPool: Max retries for url: /query?function=X&symbol=IBM&apikey={SENTINEL_KEY}&interval=5"
    cleaned = safety.scrub_text(text, secrets=[SENTINEL_KEY])
    assert SENTINEL_KEY not in cleaned and "symbol=IBM" in cleaned and "interval=5" in cleaned


def test_a_key_is_removed_even_when_the_caller_does_not_name_it_TA_58():
    text = f"GET https://api.example.com/v1/bars?symbol=AAPL&api_key={SENTINEL_KEY}&token={SENTINEL_TOKEN}"
    cleaned = safety.scrub_text(text)
    assert SENTINEL_KEY not in cleaned and SENTINEL_TOKEN not in cleaned and "symbol=AAPL" in cleaned


def test_headers_and_bearer_tokens_are_removed_TA_58():
    text = f"Authorization: Bearer {SENTINEL_TOKEN}\nX-Api-Key: {SENTINEL_KEY}\nContent-Type: application/json\nCookie: s={SENTINEL_KEY}"
    cleaned = safety.scrub_text(text)
    assert SENTINEL_TOKEN not in cleaned and SENTINEL_KEY not in cleaned and "application/json" in cleaned


def test_an_account_identifier_in_a_body_is_removed_TA_58():
    body = f'{{"accountNumber": "{SENTINEL_ACCOUNT}", "symbol": "AAPL", "hashValue": "{SENTINEL_KEY}"}}'
    cleaned = safety.scrub_text(body)
    assert SENTINEL_ACCOUNT not in cleaned and SENTINEL_KEY not in cleaned and "AAPL" in cleaned


def test_credentials_inside_a_url_authority_are_removed_TA_58():
    cleaned = safety.scrub_text("proxy error via https://user:" + SENTINEL_KEY + "@proxy.example.com:8080/")
    assert SENTINEL_KEY not in cleaned and "proxy.example.com" in cleaned


def test_nested_values_are_scrubbed_before_the_model_or_the_log_TA_58():
    payload = {"url": f"https://x/?key={SENTINEL_KEY}", "headers": {"Authorization": f"Bearer {SENTINEL_TOKEN}"},
               "rows": [{"note": f"acct {SENTINEL_ACCOUNT}"}, 3, None], "symbol": "AAPL"}
    cleaned = safety.scrub_value(payload, secrets=[SENTINEL_ACCOUNT])
    flat = repr(cleaned)
    assert SENTINEL_KEY not in flat and SENTINEL_TOKEN not in flat and SENTINEL_ACCOUNT not in flat
    assert cleaned["symbol"] == "AAPL" and cleaned["rows"][1] == 3


def test_an_ordinary_word_that_holds_key_is_left_alone_TA_58():
    assert safety.scrub_text("monkey=1 and keyboard") == "monkey=1 and keyboard"


def test_call_scrubbed_reraises_without_the_secret_or_the_chain_TA_58():
    def failing():
        raise ConnectionError(f"failed for https://x/?apikey={SENTINEL_KEY}")

    with pytest.raises(safety.ScrubbedError) as info:
        safety.call_scrubbed(failing, secrets=[SENTINEL_KEY])
    error = info.value
    assert SENTINEL_KEY not in str(error) and error.kind == "ConnectionError"
    assert error.__cause__ is None and error.__suppress_context__ is True


def test_call_scrubbed_returns_the_value_and_passes_selected_errors_through_TA_58():
    assert safety.call_scrubbed(lambda a, b=1: a + b, 2, b=3) == 5
    with pytest.raises(KeyError):
        safety.call_scrubbed(lambda: {}["x"], passthrough=(KeyError,))
