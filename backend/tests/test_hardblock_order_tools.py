"""The lock of the order tools (SPEC-015 18.7): the four exact MCP tools of Hokma's own server pass the order-name pattern of
the always-on rule (and nothing else does), the hosts and the secrets stay refused for them, the server name `hokma` is
reserved, an agent still cannot reach the order routes of Schwab, and only the broker service has an exception for them."""
import json
import re
from pathlib import Path

import pytest
from hardblock_support import box  # noqa: F401  (fixture)

import hokma
from hokma.kit import hardblock, mcp_config
from hokma.permissions.broker import BrokerCtx, check

ROOT = Path(hokma.__file__).parent
HOST = "api.schwabapi.com"
HOKMA_ORDER_TOOLS = ("mcp__hokma__order_place", "mcp__hokma__order_replace", "mcp__hokma__order_cancel",
                     "mcp__hokma__order_preview")
ORDER = {"orderType": "LIMIT", "session": "NORMAL", "duration": "DAY", "price": "210.50", "orderStrategyType": "SINGLE",
         "orderLegCollection": [{"instruction": "SELL", "quantity": 10, "instrument": {"symbol": "AAPL", "assetType": "EQUITY"}}]}


@pytest.fixture
def bctx(box):
    return BrokerCtx(data=str(box.data), roots=[str(box.work)], protect=[str(box.data)], conversation_id="7")


def args_for(tool: str) -> dict:
    base = {"desk_id": 3, "order": ORDER, "reason": "take profit after the close"}
    return {"mcp__hokma__order_cancel": {"desk_id": 3, "order_id": 123, "reason": "stale"}}.get(tool, base)


# ------------------------------------------------------------------------------------------- the list

def test_the_list_is_exactly_the_four_tools_of_the_spec():
    assert hardblock.load_brokers()["hokma_order_tools"] == list(HOKMA_ORDER_TOOLS)


@pytest.mark.parametrize("tool", HOKMA_ORDER_TOOLS)
def test_the_hook_lets_the_four_hokma_order_tools_pass(box, tool):
    assert box.hit(tool, **args_for(tool)) is None
    done = box.run_hook({"tool_name": tool, "tool_input": args_for(tool), "cwd": str(box.work)})
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize("tool", HOKMA_ORDER_TOOLS)
def test_the_permission_broker_lets_the_four_hokma_order_tools_pass(box, bctx, tool):
    assert check(tool, args_for(tool), cwd=str(box.work), ctx=bctx).allowed
    assert check("mcp", {"server": "hokma", "tool": tool.split("__")[2], "arguments": args_for(tool)},
                 cwd=str(box.work), ctx=bctx).allowed


def test_a_reason_that_names_an_order_tool_does_not_block_a_listed_tool(box, bctx):
    """The arguments of a listed tool are not read with the order-name pattern: a reason like 'cancel_order failed' is text."""
    tool = "mcp__hokma__order_place"
    arguments = {"desk_id": 3, "order": ORDER, "reason": "the earlier cancel_order and place_order calls timed out"}
    assert box.hit(tool, **arguments) is None
    assert check(tool, arguments, cwd=str(box.work), ctx=bctx).allowed


@pytest.mark.parametrize("tool", [
    "mcp__hokma__place_order", "mcp__hokma__cancel_order", "mcp__hokma__submit_order", "mcp__hokma__order_create",
    "mcp__hokma__order_send", "mcp__hokma__create_order", "mcp__hokma__modify_order", "mcp__hokma__replace_order",
    "mcp__otro__place_order", "mcp__otro__order_cancel", "mcp__otro__cancel_order", "mcp__otro__replace_order",
    "mcp__broker__submit_order", "mcp__plugin_x_y__place_order",
    "mcp__Hokma__order_cancel", "mcp__hokma__Order_Cancel", "mcp__hokma__order_cancel2", "mcp__hokma_x__order_cancel",
    "mcp__hokma__order_send_all",
])
def test_every_other_order_tool_stays_blocked(box, bctx, tool):
    assert box.hit(tool, desk_id=1) == "real_money"
    assert not check(tool, {"desk_id": 1}, cwd=str(box.work), ctx=bctx).allowed


@pytest.mark.parametrize("tool", ["mcp__otro__place_order", "mcp__hokma__place_order", "mcp__x__order_cancel"])
def test_the_hook_as_a_process_refuses_them(box, tool):
    done = box.run_hook({"tool_name": tool, "tool_input": {}, "cwd": str(box.work)})
    assert done.returncode == 2 and "HOKMA_BLOCKED real_money" in done.stderr


def test_a_case_variant_of_a_listed_name_does_not_pass_the_broker(box, bctx):
    """The broker lowercases MCP names; a listed name is accepted only in its exact spelling."""
    for variant in ("mcp__Hokma__order_cancel", "MCP__HOKMA__ORDER_CANCEL", "mcp__hokma__ORDER_cancel"):
        verdict = check(variant, {"desk_id": 1}, cwd=str(box.work), ctx=bctx)
        assert (verdict.allowed, verdict.rule) == (False, "real_money"), variant
    generic = check("mcp", {"server": "Hokma", "tool": "order_cancel", "arguments": {}}, cwd=str(box.work), ctx=bctx)
    assert generic.allowed is False and generic.rule == "real_money"


def test_the_pattern_has_the_verb_on_either_side_of_order():
    assert hardblock.ORDER_TOOL.pattern == (r"(?:place|submit|send|create|modify|replace|cancel|amend|preview)"
                                            r"[_-]?order(?!s?[_-]?book)|order[_-]?"
                                            r"(?:place|submit|send|create|modify|replace|cancel|amend|preview)")
    assert hardblock.ORDER_TOOL.search("mcp__hokma__order_cancel")       # which is why the exact list exists


VERBS = ("place", "replace", "submit", "modify", "preview", "create", "send", "amend", "cancel")


@pytest.mark.parametrize("verb", VERBS)
@pytest.mark.parametrize("shape", ["order_{v}", "order-{v}", "ORDER_{V}", "Order_{V}", "order{V}", "{v}_order", "{v}-order",
                                   "{V}Order", "{v}order", "{v}_orders", "PLACE_ORDER"])
@pytest.mark.parametrize("server", ["otro", "broker", "Hokma", "hokma_x", "plugin_acme_trades"])
def test_every_order_verb_in_every_spelling_is_blocked_for_any_other_server(box, bctx, verb, shape, server):
    tool = f"mcp__{server}__" + shape.format(v=verb, V=verb.capitalize())
    assert box.hit(tool, desk_id=1) == "real_money", tool
    assert not check(tool, {"desk_id": 1}, cwd=str(box.work), ctx=bctx).allowed, tool


@pytest.mark.parametrize("tool", [
    "mcp__otro__order_place", "mcp__otro__order_replace", "mcp__otro__order_submit", "mcp__otro__order_modify",
    "mcp__otro__order_preview", "mcp__otro__order_amend", "mcp__otro__placeOrder", "mcp__otro__orderPlace",
    "mcp__otro__ORDER_PLACE", "mcp__otro__previewOrder", "mcp__otro__amend_order", "mcp__otro__order_send",
    "mcp__hokma__order_amend", "mcp__hokma__order_submit", "mcp__hokma__order_modify", "mcp__hokma__orderPlace",
    "mcp__hokma__Order_Preview", "mcp__hokma__order_place_all", "mcp__hokma__order_preview2"])
def test_the_new_names_are_blocked_by_the_hook_the_broker_and_the_process(box, bctx, tool):
    assert box.hit(tool, desk_id=1) == "real_money"
    assert check(tool, {"desk_id": 1}, cwd=str(box.work), ctx=bctx).rule == "real_money"
    done = box.run_hook({"tool_name": tool, "tool_input": {}, "cwd": str(box.work)})
    assert done.returncode == 2 and "HOKMA_BLOCKED real_money" in done.stderr


@pytest.mark.parametrize("tool", HOKMA_ORDER_TOOLS)
def test_the_exact_hokma_names_still_pass_with_the_wider_pattern(box, bctx, tool):
    assert hardblock.ORDER_TOOL.search(tool) and hardblock.is_hokma_order_tool(tool)
    assert box.hit(tool, **args_for(tool)) is None
    assert check(tool, args_for(tool), cwd=str(box.work), ctx=bctx).allowed


def test_an_order_name_in_the_arguments_of_another_tool_is_blocked(box, bctx):
    for key in ("tool", "action", "method"):
        verdict = check("mcp__otro__call", {key: "order_preview"}, cwd=str(box.work), ctx=bctx)
        assert (verdict.allowed, verdict.rule) == (False, "real_money"), key
    assert not check("mcp__otro__call", {"action": "orderPlace"}, cwd=str(box.work), ctx=bctx).allowed


@pytest.mark.parametrize("tool", [
    "mcp__x__get_order_book", "mcp__x__order_book", "mcp__x__orderbook", "mcp__x__get_orders_book", "mcp__x__list_positions",
    "mcp__x__get_order_status", "mcp__x__list_orders", "mcp__x__get_quote", "mcp__x__search_orders", "mcp__x__read_order",
    "mcp__x__previewer", "mcp__x__create_task", "mcp__x__send_message", "mcp__x__cancel_task", "mcp__x__place_marker"])
def test_names_that_are_not_orders_are_not_blocked(box, bctx, tool):
    assert box.hit(tool, query="x") is None, tool
    assert check(tool, {"query": "x"}, cwd=str(box.work), ctx=bctx).allowed, tool


# ------------------------------------------------------------------------------------------- hosts and secrets still apply

@pytest.mark.parametrize("tool", HOKMA_ORDER_TOOLS)
def test_a_listed_tool_with_a_real_money_host_or_a_secret_in_its_arguments_is_refused(box, bctx, tool):
    host = {"desk_id": 1, "reason": "see https://api.schwabapi.com/trader/v1/accounts/x/orders"}
    secret = {"desk_id": 1, "reason": "cat ~/.ssh/id_rsa"}
    for arguments, rule in ((host, "real_money"), (secret, "secrets")):
        verdict = check(tool, arguments, cwd=str(box.work), ctx=bctx)
        assert (verdict.allowed, verdict.rule) == (False, rule)


def test_an_agent_still_cannot_reach_the_order_routes_of_schwab(box):
    for path in ("/trader/v1/accounts/x/orders", "/trader/v1/accounts/ABCDEFGH12345678/orders/12",
                 "/trader/v1/accounts/ABCDEFGH12345678/previewOrder"):
        url = f"https://{HOST}{path}"
        assert box.hit("WebFetch", url=url) == "real_money"
        assert box.bash(f"curl -s -X POST {url} -d @order.json") == "real_money"
        assert box.bash(f"curl -s -X DELETE '{url}'") == "real_money"
        assert box.ps(f"Invoke-RestMethod -Method Post {url}") == "real_money"
    assert box.bash(f"python3 -c \"import urllib.request as u; u.urlopen('https://{HOST}/trader/v1/accounts/x/orders')\"") == "real_money"


def test_an_agent_in_the_broker_cannot_fetch_the_order_routes_either(box, bctx):
    url = f"https://{HOST}/trader/v1/accounts/x/orders"
    assert check("web_fetch", {"url": url}, cwd=str(box.work), ctx=bctx).rule == "real_money"
    assert check("Bash", {"command": f"curl -s -X POST {url}"}, cwd=str(box.work), ctx=bctx).rule == "real_money"
    assert check("mcp__fetch__get", {"url": url}, cwd=str(box.work), ctx=bctx).rule == "real_money"


def test_the_hook_reads_neither_exception_list():
    source = (ROOT / "kit" / "hardblock.py").read_text(encoding="utf-8")
    body = source.split("def evaluate", 1)[1]
    assert "order_exception" not in body and "read_exception" not in body and "exception_match" not in body
    ctx_source = source.split("class Ctx", 1)[1].split("def _build_vars", 1)[0]
    assert "order_exceptions" not in ctx_source and "read_exceptions" not in ctx_source


# ------------------------------------------------------------------------------------------- the reserved server name

@pytest.mark.parametrize("name", ["hokma", "Hokma", "HOKMA", "hOkMa", " hokma "])
def test_a_server_of_another_origin_cannot_take_the_name_hokma(name):
    assert mcp_config.is_reserved(name)
    with pytest.raises(ValueError):
        mcp_config.validate_mcp({"command": "npx", "args": ["-y", "pkg"]}, name=name)
    with pytest.raises(ValueError):
        mcp_config.validate_mcp({"type": "http", "url": "https://example.com/mcp"}, name=name)
    assert mcp_config.SERVER_NAME.match(name) is None


@pytest.mark.parametrize("name", ["hokma2", "my-hokma", "hokma-x", "hokmas", "okma", "github"])
def test_other_names_are_still_fine(name):
    assert not mcp_config.is_reserved(name)
    assert mcp_config.SERVER_NAME.match(name)
    assert mcp_config.validate_mcp({"command": "npx", "args": ["-y", "pkg"]}, name=name)["command"] == "npx"


def test_the_shape_check_without_a_name_is_as_before():
    assert mcp_config.validate_mcp({"command": "npx"}) == {"type": "stdio", "command": "npx", "args": []}


def test_the_callers_that_register_servers_refuse_the_name_hokma():
    from hokma.kit import extensions, requests

    with pytest.raises(ValueError):
        extensions.set_mcp("hokma", {"command": "npx"})
    with pytest.raises(requests.RequestError):
        requests._validate("mcp", "hokma", {"command": "npx"})
    with pytest.raises(requests.RequestError):
        requests._validate("mcp", "Hokma", {"command": "npx"})


def test_the_server_of_hokma_is_built_by_launch_not_registered():
    source = (ROOT / "kit" / "launch.py").read_text(encoding="utf-8")
    assert 'servers[HOKMA_SERVER] = hokma_server()' in source


# ------------------------------------------------------------------------------------------- order_exceptions

OK = "ABCDEFGH12345678"
ROUTES = [("POST", "/trader/v1/accounts/{a}/orders"), ("PUT", "/trader/v1/accounts/{a}/orders/{o}"),
          ("DELETE", "/trader/v1/accounts/{a}/orders/{o}"), ("POST", "/trader/v1/accounts/{a}/previewOrder")]


def routes():
    return [(m, p.format(a=OK, o="123456789012")) for m, p in ROUTES]


@pytest.mark.parametrize("method, path", routes())
def test_order_exception_allows_the_four_routes_for_the_broker_service_only(method, path):
    assert hardblock.order_exception_allows(HOST, method, path, "hokma_broker")
    assert hardblock.order_exception_allows(HOST, method.lower(), path + "?x=1", "hokma_broker")
    for consumer in ("hokma_backend", "agent", "hokma_agent", "", "Hokma_Broker", "hokma_broker2", "hokma_broker "):
        assert not hardblock.order_exception_allows(HOST, method, path, consumer), consumer
    for other in ("GET", "PATCH", "HEAD", "OPTIONS", "POST" if method != "POST" else "PUT"):
        if (other, path) not in routes():
            assert not hardblock.order_exception_allows(HOST, other, path, "hokma_broker"), other


def test_the_list_has_exactly_the_four_rules_of_18_7():
    rules = hardblock.load_brokers()["order_exceptions"]
    assert sorted((r["method"], r["path"]) for r in rules) == sorted(
        (m, p.format(a="{accountNumber}", o="{orderId}")) for m, p in ROUTES)
    assert all(r["host"] == HOST and r["consumer"] == "hokma_broker" and r["match"] == "path_template" for r in rules)
    account = "(?!accountNumbers$)[A-Za-z0-9]{8,128}"
    assert all(r["params"]["accountNumber"] == account for r in rules)
    assert [r["params"]["orderId"] for r in rules if "orderId" in r["params"]] == ["[0-9]{1,19}"] * 2


@pytest.mark.parametrize("path", [
    "/trader/v1/orders", "/trader/v1/accounts/orders", "/trader/v1/accounts/accountNumbers/orders",
    "/trader/v1/accounts/accountNumbers/previewOrder", f"/trader/v1/accounts/{OK}", f"/trader/v1/accounts/{OK}/orders/",
    f"/trader/v1/accounts/{OK}/orders/12a", f"/trader/v1/accounts/{OK}/orders/" + "1" * 20, f"/trader/v1/accounts/{OK}/orders/-1",
    f"/trader/v1/accounts/{OK}/orders/1/cancel", f"/trader/v1/accounts/{OK}/orders/1/extra", f"/trader/v1/accounts/{OK}/Orders",
    f"/trader/v1/accounts/{OK}/previewOrder/1", f"/trader/v1/accounts/{OK}/preview", "/trader/v1/accounts/AB/orders",
    f"/trader/v1/accounts/{'A' * 129}/orders", "/trader/v1/accounts/ABC-EFGH1/orders", "/trader/v1/accounts//orders",
    f"/trader/v1/accounts/{OK}//orders", f"/trader/v1//accounts/{OK}/orders", f"/Trader/v1/accounts/{OK}/orders",
    f"/trader/v1/accounts/{OK}/transactions", "/v1/oauth/token", "/marketdata/v1/quotes", "/trader/v1/userPreference",
    f"/trader/v1/accounts/{OK}/orders/1/", f"/trader/v1/accounts/{OK}/positions",
])
def test_nothing_else_is_allowed_for_the_broker_service(path):
    for method in ("POST", "PUT", "DELETE", "GET", "PATCH"):
        assert not hardblock.order_exception_allows(HOST, method, path, "hokma_broker"), (method, path)


def test_the_host_must_be_schwab():
    for host in ("api.schwabapi.com.evil.com", "evil.api.schwabapi.com", "api.alpaca.markets", "", "schwabapi.com"):
        assert not hardblock.order_exception_allows(host, "POST", f"/trader/v1/accounts/{OK}/orders", "hokma_broker")
    assert hardblock.order_exception_allows("API.SCHWABAPI.COM", "POST", f"/trader/v1/accounts/{OK}/orders", "hokma_broker")


def test_a_read_rule_never_authorizes_a_write_and_the_other_way_round():
    read = hardblock.load_brokers()["read_exceptions"]
    order = hardblock.load_brokers()["order_exceptions"]
    assert not any(r["consumer"] == "hokma_broker" for r in read)
    assert not any(r["consumer"] == "hokma_backend" for r in order)
    # the reads are not orders
    for method, path in (("GET", f"/trader/v1/accounts/{OK}/orders"), ("GET", f"/trader/v1/accounts/{OK}/orders/12"),
                         ("GET", "/trader/v1/accounts"), ("POST", "/v1/oauth/token")):
        assert not hardblock.order_exception_allows(HOST, method, path, "hokma_broker"), (method, path)
        assert not hardblock.order_exception_allows(HOST, method, path, "hokma_backend")
        assert hardblock.exception_match(HOST, method, path, "hokma_broker") is None
    # the orders are not reads
    for method, path in routes():
        assert not hardblock.read_exception_allows(HOST, method, path, "hokma_broker")
        assert not hardblock.read_exception_allows(HOST, method, path, "hokma_backend")
        assert hardblock.exception_match(HOST, method, path, "hokma_backend") is None
        assert hardblock.exception_match(HOST, method, path, "hokma_broker") is not None


def test_a_rule_in_the_wrong_list_is_not_honoured():
    """A rule put in read_exceptions with the broker consumer, or the reverse, authorizes nothing."""
    rule = {"host": HOST, "method": "POST", "path": "/trader/v1/accounts/{a}/orders", "match": "path_template",
            "consumer": "hokma_broker", "params": {"a": "[A-Za-z0-9]{8,128}"}}
    path = f"/trader/v1/accounts/{OK}/orders"
    only_read = {"read_exceptions": [rule]}
    only_order = {"order_exceptions": [dict(rule, consumer="hokma_backend")]}
    assert not hardblock.order_exception_allows(HOST, "POST", path, "hokma_broker", only_read)
    assert not hardblock.exception_match(HOST, "POST", path, "hokma_broker", only_read)
    assert not hardblock.exception_match(HOST, "POST", path, "hokma_backend", only_order)
    assert not hardblock.order_exception_allows(HOST, "POST", path, "hokma_backend", only_order)
    own = {"order_exceptions": [rule]}
    assert hardblock.order_exception_allows(HOST, "POST", path, "hokma_broker", own)
    assert not hardblock.order_exception_allows(HOST, "POST", path, "hokma_broker", {"order_exceptions": [dict(rule, match="exact_path_query_allowed")]})


def test_the_consumer_name_of_the_broker_is_written_only_where_it_belongs():
    named = sorted(str(p.relative_to(ROOT)).replace("\\", "/") for p in ROOT.rglob("*")
                   if p.is_file() and "__pycache__" not in p.parts and p.suffix in (".py", ".json", ".ts", ".md", ".txt")
                   and "hokma_broker" in p.read_text(encoding="utf-8", errors="ignore"))
    assert named == ["kit/hardblock.py", "kit/hardblock_brokers.json", "schwab/orders.py"]
    assert not re.search(r"hokma_broker", (ROOT / "schwab" / "http.py").read_text(encoding="utf-8"))
