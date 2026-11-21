"""The `path_template` rule of `read_exceptions` (SPEC-015 5.8): a marker is one path segment that fully matches its regex,
the segment count and the literal segments are fixed, the method, host and consumer are compared exactly."""
import pytest

from hokma.kit import hardblock
from hokma.kit.hardblock import _template_matches, read_exception_allows, read_exception_match

RULE = {"host": "api.example.com", "method": "GET", "path": "/v1/{kind}/{id}/items", "match": "path_template",
        "consumer": "hokma_backend", "params": {"kind": "a|b", "id": "[0-9]{1,3}"}}
BROKERS = {"read_exceptions": [RULE]}


def allows(path, method="GET", host="api.example.com", consumer="hokma_backend", brokers=BROKERS):
    return read_exception_allows(host, method, path, consumer, brokers)


def test_a_path_that_fits_the_template_passes():
    assert allows("/v1/a/1/items") and allows("/v1/b/999/items") and allows("/v1/a/1/items?x=1")
    assert read_exception_match("api.example.com", "get", "/v1/a/1/items", "hokma_backend", BROKERS) is RULE


@pytest.mark.parametrize("path", ["/v1/a/b/1/items", "/v1/a/1/items/x", "/v1/a/items", "/v1/a/1", "/v1", "/v1/a//items",
                                  "/v1//1/items", "/v1/a/1/items/extra/more"])
def test_more_or_fewer_segments_do_not_pass(path):
    assert not allows(path)


@pytest.mark.parametrize("path", ["/v1/a/1/2/items", "/v1/a%2F1/items", "/v1/a/1/itemsX", "/v2/a/1/items",
                                  "/V1/a/1/items", "/v1/a/1/Items"])
def test_a_marker_never_spans_a_slash_and_literals_are_exact(path):
    assert not allows(path)


@pytest.mark.parametrize("value", ["c", "ab", "aa", "A", "", "a ", " a", "a|b"])
def test_the_regex_must_match_the_whole_segment(value):
    assert not allows(f"/v1/{value}/1/items")


@pytest.mark.parametrize("value", ["1234", "12a", "-1", "", "1 "])
def test_the_id_marker_has_its_own_regex(value):
    assert not allows(f"/v1/a/{value}/items")


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", ""])
def test_the_method_is_compared_exactly(method):
    assert not allows("/v1/a/1/items", method=method)


def test_host_and_consumer_are_compared_exactly():
    assert not allows("/v1/a/1/items", host="api.example.org")
    assert not allows("/v1/a/1/items", host="x.api.example.com")
    assert allows("/v1/a/1/items", host="API.EXAMPLE.COM")
    assert not allows("/v1/a/1/items", consumer="agent")
    assert not allows("/v1/a/1/items", consumer="")


def test_a_marker_without_a_regex_or_with_a_bad_one_never_passes():
    for params in ({}, {"kind": "a|b"}, {"kind": 5, "id": "[0-9]+"}, None):
        brokers = {"read_exceptions": [{**RULE, "params": params}]}
        assert not allows("/v1/a/1/items", brokers=brokers)


def test_a_rule_of_another_kind_does_not_use_the_template():
    brokers = {"read_exceptions": [{**RULE, "match": "prefix"}]}
    assert not allows("/v1/a/1/items", brokers=brokers)
    exact = {"read_exceptions": [{**RULE, "match": "exact_path_query_allowed"}]}
    assert not allows("/v1/a/1/items", brokers=exact)
    assert allows("/v1/{kind}/{id}/items", brokers=exact)


def test_a_marker_inside_a_segment_is_literal_text():
    assert _template_matches("/v1/x{id}", {"id": "[0-9]+"}, "/v1/x{id}")
    assert not _template_matches("/v1/x{id}", {"id": "[0-9]+"}, "/v1/x12")


def test_an_exact_rule_wins_over_a_template_that_also_matches():
    brokers = {"read_exceptions": [
        {**RULE, "path": "/v1/accounts/{h}", "params": {"h": "(?!numbers$)[a-z]{4,10}"}},
        {**RULE, "path": "/v1/accounts/numbers", "match": "exact_path_query_allowed"}]}
    assert allows("/v1/accounts/numbers", brokers=brokers) and allows("/v1/accounts/abcdef", brokers=brokers)
    assert not allows("/v1/accounts/numbers/x", brokers=brokers)
    only_template = {"read_exceptions": [brokers["read_exceptions"][0]]}
    assert not allows("/v1/accounts/numbers", brokers=only_template)


def test_the_shipped_file_has_templates_with_a_regex_for_every_marker():
    import re

    rules = [r for r in hardblock.load_brokers()["read_exceptions"] if r.get("match") == "path_template"]
    assert rules
    for rule in rules:
        markers = re.findall(r"\{(\w+)\}", rule["path"])
        assert markers and set(markers) == set(rule["params"]), rule["path"]
        assert all(isinstance(p, str) and p for p in rule["params"].values())
        assert rule["method"] == "GET" and rule["consumer"] == "hokma_backend"


# ------------------------------------------------------------------------------------------- dot and encoded segments (C1)

WIDE = {"host": "api.example.com", "method": "GET", "path": "/v1/{sym}/quotes", "match": "path_template",
        "consumer": "hokma_backend", "params": {"sym": "[A-Za-z0-9.%^-]{1,32}"}}                  # the real symbol regex lets `.` and `%` in
WIDE_BROKERS = {"read_exceptions": [WIDE]}
WIDE_ORDERS = {"order_exceptions": [{**WIDE, "consumer": "hokma_broker", "method": "POST", "path": "/v1/{sym}/orders"}]}

DOTS = [".", "..", "%2e", "%2E", "%2e%2e", "%2E%2E", "%2e.", ".%2e", "..%2f", "%2e%2e%2f", "%2e%2e%2F", "%2f", "%2F", "a%2fb",
        "%252e", "%252E", "%252e%252e", "%252f", "%252F", "%25252e", "%2525252e", "%5c", "%5C", "%2e%5c", "%255c",
        "%252e%252e%252f", "a%2Fb%2e%2e"]
FREE = ["SPY", "BRK.B", "%24SPX", "%24DJI", "a.b", "A-B", "%5EGSPC", "1.5", "...", ".a", "a.", "%25", "%2524SPX"]


@pytest.mark.parametrize("segment", DOTS)
def test_a_marker_never_takes_a_dot_segment_or_an_encoded_dot_or_slash(segment):
    assert hardblock.unsafe_segment(segment)
    assert not read_exception_allows("api.example.com", "GET", f"/v1/{segment}/quotes", "hokma_backend", WIDE_BROKERS)
    assert not hardblock.order_exception_allows("api.example.com", "POST", f"/v1/{segment}/orders", "hokma_broker", WIDE_ORDERS)
    assert not hardblock.exception_match("api.example.com", "GET", f"/v1/{segment}/quotes", "hokma_backend", WIDE_BROKERS)
    assert not hardblock.exception_match("api.example.com", "POST", f"/v1/{segment}/orders", "hokma_broker", WIDE_ORDERS)
    assert not _template_matches("/v1/{sym}/quotes", WIDE["params"], f"/v1/{segment}/quotes")


@pytest.mark.parametrize("segment", FREE)
def test_a_plain_or_once_encoded_symbol_still_passes(segment):
    assert not hardblock.unsafe_segment(segment)
    assert read_exception_allows("api.example.com", "GET", f"/v1/{segment}/quotes", "hokma_backend", WIDE_BROKERS)
    assert hardblock.order_exception_allows("api.example.com", "POST", f"/v1/{segment}/orders", "hokma_broker", WIDE_ORDERS)


def test_the_literal_segments_of_the_template_are_unchanged():
    assert _template_matches("/v1/{sym}/quotes", WIDE["params"], "/v1/SPY/quotes")
    assert not _template_matches("/v1/{sym}/quotes", WIDE["params"], "/v1/SPY/%2e%2e")
    assert not _template_matches("/v1/{sym}/quotes", WIDE["params"], "/v1/SPY/Quotes")


def test_the_shipped_symbol_rules_refuse_them_too():
    host = "api.schwabapi.com"
    for segment in DOTS:
        for path in (f"/marketdata/v1/{segment}/quotes", f"/marketdata/v1/movers/{segment}",
                     f"/marketdata/v1/instruments/{segment}", f"/trader/v1/accounts/{segment}"):
            assert not hardblock.read_exception_allows(host, "GET", path, "hokma_backend"), path
        for method, path in (("POST", f"/trader/v1/accounts/{segment}/orders"), ("PUT", f"/trader/v1/accounts/{segment}/orders/1"),
                             ("DELETE", f"/trader/v1/accounts/AB12CD34/orders/{segment}"),
                             ("POST", f"/trader/v1/accounts/{segment}/previewOrder")):
            assert not hardblock.order_exception_allows(host, method, path, "hokma_broker"), path
    assert hardblock.read_exception_allows(host, "GET", "/marketdata/v1/%24SPX/quotes", "hokma_backend")
    assert hardblock.read_exception_allows(host, "GET", "/marketdata/v1/BRK.B/quotes", "hokma_backend")
