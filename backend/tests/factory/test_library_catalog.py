"""The shared library (SPEC-032: OPT-16): one list read from the registries that exist, with and without the filters of SPEC-030."""
import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)

import hokma.quant.indicators  # noqa: F401  (importing the package registers the indicators)
from hokma.factory import indicator_library as lib
from hokma.factory import library_catalog as cat
from hokma.factory import parts
from hokma.factory import registry as factory_registry
from hokma.factory.params import Param, ParamSchema
from hokma.factory.spec import default_registry
from hokma.quant import registry as quant_registry

FIRST = ("ema", "sma", "rsi", "macd_line", "macd_signal", "adx", "atr", "half_life", "ewma_vol")


def names(items):
    return [i.name for i in items]


def test_the_nine_of_the_library_are_ready_indicators_and_so_is_every_row_of_the_wiring(db):
    ready = {i.name: i for i in cat.catalog(kind="indicator", status="ready")}
    assert set(ready) == set(lib.LIBRARY) and set(FIRST) <= set(ready)
    assert all(i.source == "indicator_library" and i.fields and i.description for i in ready.values())
    assert ready["ema"].fields == ("close",) and ready["adx"].fields == ("high", "low", "close")
    assert [p["name"] for p in ready["macd_signal"].params] == ["fast", "slow", "signal"]
    bollinger = ready["bollinger_upper"]
    assert bollinger.fields == ("close",) and bollinger.params == ({"name": "length", "type": "int", "default": 20},)


def test_the_h3_indicators_the_factory_does_not_hand_out_are_h3_only_and_nothing_is_left_out(db):
    quant_registry.load_all()
    h3_only = cat.catalog(kind="indicator", status="h3_only")
    assert h3_only and all(i.source == "quant.indicators" and i.description for i in h3_only)
    used = {w.h3 for w in lib.WIRING} | {"indicators.ema", "indicators.sma", "indicators.rsi", "indicators.macd", "indicators.adx", "indicators.atr"}
    every = {s.name for s in quant_registry.list(module="indicators")}
    assert {f"indicators.{i.name}" for i in h3_only} == every - used
    assert "butterworth" in names(h3_only) and "ema" not in names(h3_only) and "bollinger" not in names(h3_only)


def test_the_alphas_and_the_parts_come_from_their_registries(db):
    alphas = cat.catalog(kind="alpha")
    assert names(alphas) == sorted(r["id"] for r in factory_registry.factory_list(default_registry(), kind="alpha")) and alphas
    assert all(i.status == "ready" and i.source == "factory.registry" for i in alphas)
    for kind, table, source in (("sizer", parts.SIZERS, "parts.SIZERS"), ("exit", parts.EXITS, "parts.EXITS"), ("execution", parts.EXECUTORS, "parts.EXECUTORS")):
        items = cat.catalog(kind=kind)
        assert names(items) == sorted(table) and all(i.source == source and i.status == "ready" for i in items)
    twap = next(i for i in cat.catalog(kind="execution") if i.name == "twap")
    assert twap.params == ({"name": "n_slices", "type": "int", "low": 2, "high": 100},)


def test_it_works_without_parts_filters_and_lists_them_when_the_module_has_them(db, monkeypatch):
    monkeypatch.delattr(parts, "FILTERS", raising=False)
    assert cat.catalog(kind="filter") == []
    assert names(cat.catalog(kind="sizer")) == sorted(parts.SIZERS)
    fake = parts.PartKind("trend_gate", ParamSchema((Param("length", "int", default=20, low=2, high=500),)), lambda v, env: None, "Only trade with the trend.")
    monkeypatch.setattr(parts, "FILTERS", {"trend_gate": fake}, raising=False)
    got = cat.catalog(kind="filter")
    assert names(got) == ["trend_gate"] and got[0].source == "parts.FILTERS" and got[0].status == "ready"
    assert got[0].params == ({"name": "length", "type": "int", "low": 2, "high": 500, "default": 20},)
    assert "trend_gate" in names(cat.catalog())


def test_the_filters_by_kind_and_status_and_a_wrong_one_is_bad_input(db):
    everything = cat.catalog()
    assert {i.kind for i in everything} >= {"indicator", "alpha", "sizer", "exit", "execution"}
    assert {i.status for i in everything} >= {"ready", "h3_only"}
    assert names(cat.catalog(status="h3_only")) == names(cat.catalog(kind="indicator", status="h3_only"))
    assert len(everything) == len({(i.kind, i.name) for i in everything})
    assert [i.kind for i in everything] == sorted((i.kind for i in everything), key=cat.KINDS.index)
    for bad in ({"kind": "bot"}, {"status": "done"}):
        with pytest.raises(cat.LibraryError) as err:
            cat.catalog(**bad)
        assert err.value.code == "bad_input"


def test_without_the_table_of_requests_nothing_is_requested_and_nothing_fails(env):
    assert cat.catalog(status="requested") == [] and cat.requests() == []
    assert len(cat.catalog()) > 0


# ---- OPT-18: requests for an indicator that is missing ----

WHY = "A rolling Hurst exponent of the close over a window, to tell trend from mean reversion on minute bars."


def asked(db, **over):
    base = dict(description=WHY, fields=("close",), params={"length": 100}, bank_version_id=None, caller="ian")
    return cat.request(over.pop("name", "hurst"), **{**base, **over}, db=db)


def count_requests(db) -> int:
    with db.read() as u:
        return u.session.scalar("SELECT COUNT(*) FROM library_requests")


def test_a_name_that_is_ready_is_bad_input_and_the_error_carries_its_card(db):
    for name in ("ema", "bollinger_upper"):
        with pytest.raises(cat.LibraryError, match="already exists") as err:
            asked(db, name=name)
        assert err.value.code == "bad_input" and err.value.item.name == name and err.value.item.status == "ready"
    assert count_requests(db) == 0


def test_a_name_that_is_h3_only_is_saved_and_the_notice_says_it_only_needs_wiring(db):
    assert cat.request_notice("butterworth") and "only needs to be wired" in cat.request_notice("butterworth")
    assert cat.request_notice("hurst") is None and cat.request_notice("ema") is None
    request_id = asked(db, name="butterworth", fields=("close",), params={})
    assert [r["id"] for r in cat.requests(status="open")] == [request_id]
    assert "butterworth" in names(cat.catalog(kind="indicator", status="h3_only")) and "butterworth" in names(cat.catalog(status="requested"))


def test_a_new_name_is_an_open_request_that_the_catalog_and_the_list_show(db):
    request_id = asked(db, fields=("high", "close"), params={"length": 100, "smooth": True}, caller="ian")
    row, = cat.requests()
    assert row["id"] == request_id and (row["name"], row["status"], row["requested_by"], row["resolution"]) == ("hurst", "open", "ian", None)
    assert row["fields"] == ["high", "close"] and row["params"] == {"length": 100, "smooth": True} and row["description"] == WHY
    assert row["bank_version_id"] is None and row["created_at"]
    item, = cat.catalog(status="requested")
    assert (item.name, item.kind, item.source, item.fields, item.description) == ("hurst", "indicator", "library_requests", ("high", "close"), WHY)
    assert {p["name"] for p in item.params} == {"length", "smooth"}
    assert cat.requests(status="wired") == [] and [r["id"] for r in cat.requests(status="open")] == [request_id]
    with pytest.raises(cat.LibraryError):
        cat.requests(status="done")


def test_the_limits_of_the_text_are_enforced_and_a_refused_request_writes_nothing(db):
    assert asked(db, name="n" * 48, description="d" * 20) and asked(db, name="m", description="d" * 2000)
    before = count_requests(db)
    for bad in ({"name": ""}, {"name": "n" * 49}, {"name": 7}, {"description": "d" * 19}, {"description": " " * 30}, {"description": "d" * 2001},
                {"caller": ""}, {"fields": ("close", "close")}, {"fields": ("price",)}, {"fields": "close"}, {"params": {"x": float("nan")}},
                {"params": {"x": object()}}, {"params": ["length"]}, {"bank_version_id": 0}, {"bank_version_id": True}, {"bank_version_id": "3"}):
        with pytest.raises(cat.LibraryError) as err:
            asked(db, **bad)
        assert err.value.code == "bad_input", bad
    assert count_requests(db) == before


def test_a_bank_version_that_does_not_exist_is_bad_input_only_when_the_table_is_there(db):
    with db.write() as u:
        u.session.execute("DROP TABLE strategy_bank_versions")                          # the bank (0036) is in the base now: take it out to look at a base where SPEC-030 is not applied
    request_id = asked(db, name="no_bank_table", bank_version_id=41)                    # SPEC-030 is not applied: the id cannot be checked and is kept
    assert cat.requests()[0]["bank_version_id"] == 41 and request_id
    with db.write() as u:
        u.session.execute("CREATE TABLE IF NOT EXISTS strategy_bank_versions (id INTEGER PRIMARY KEY)")
        u.session.execute("INSERT INTO strategy_bank_versions (id) VALUES (7)")
    before = count_requests(db)
    with pytest.raises(cat.LibraryError, match="does not exist") as err:
        asked(db, name="with_bank_table", bank_version_id=8)
    assert err.value.code == "bad_input" and count_requests(db) == before
    asked(db, name="with_bank_table", bank_version_id=7)
    assert [r["bank_version_id"] for r in cat.requests()] == [41, 7]
