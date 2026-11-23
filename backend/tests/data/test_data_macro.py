"""Macro with vintage, other countries, fixed income and the reference board (SPEC-017: TA-70, FT-094, FT-095, FT-096, OA-66)."""
import json

import pandas as pd
import pytest

from hokma.data import _lib, macro as macro_module, sentinels
from hokma.data.macro import (CURATED_BOARD, COUNTRY_KINDS, FIXED_INCOME_KINDS, CachedBoard, FredProvider, build_global_macro, country_series,
                              fixed_income, macro, macro_board, normalize_global_cell, resolve_series_id, year_over_year)
from hokma.data.providers import RateLimitError

from .support import RecordedTransport, constant, fixture_bytes, fixture_json, no_network, when  # noqa: F401


def key(provider: str):
    return "FRED-SECRET"


def fred_answer(url: str, params: dict, headers: dict):
    """Two recorded vintages of the same series. The vintage is chosen by the real-time window of the request."""
    assert params["realtime_start"] == params["realtime_end"], "the vintage must be pinned to one day"
    if url.endswith("/series"):
        return 200, fixture_bytes("fred_series_meta.json")
    name = "fred_cpi_vintage_2024-03-01.json" if params["realtime_end"] <= "2024-03-01" else "fred_cpi_latest.json"
    return 200, fixture_bytes(name)


def call(series="CPIAUCSL", as_of="2024-03-01", transport=None, **kwargs):
    return macro(series, as_of=when(as_of), key=kwargs.pop("key", key), transport=transport or RecordedTransport(fred_answer),
                 clock=lambda: when("2026-11-22T12:00:00"), **kwargs)


def test_vintage_returns_the_value_published_that_day_not_the_revision_TA_70() -> None:
    old, new = call(as_of="2024-03-01"), call(as_of="2026-01-01")
    assert old.ok and new.ok
    assert {o["date"]: o["value"] for o in old.value["observations"]}["2024-01-01"] == 308.417        # as published on 2024-03-01
    assert {o["date"]: o["value"] for o in new.value["observations"]}["2024-01-01"] == 309.685        # the later revision
    assert old.value["vintage"] == "2024-03-01" and new.value["vintage"] == "2026-01-01"
    assert old.value["latest"]["date"] == "2024-01-01" and old.value["missing_observations"] == 1       # February was not published: stays missing


def test_vintage_pin_is_sent_on_both_requests_and_the_key_stays_out_of_the_result_TA_70() -> None:
    transport = RecordedTransport(fred_answer)
    result = call(transport=transport)
    assert [c[0].rsplit("/", 1)[-1] for c in transport.calls] == ["series", "observations"]
    assert all(c[1]["realtime_end"] == "2024-03-01" for c in transport.calls)
    assert transport.calls[1][1]["observation_end"] == "2024-03-01" and transport.calls[1][1]["api_key"] == "FRED-SECRET"
    assert "FRED-SECRET" not in json.dumps(result.to_dict(), default=str)


def test_an_as_of_in_the_future_is_clamped_to_the_provider_today_TA_70() -> None:
    transport = RecordedTransport(fred_answer)
    result = macro("CPIAUCSL", as_of=when("2999-01-01"), key=key, transport=transport, clock=lambda: when("2026-11-22T12:00:00"))
    assert transport.calls[0][1]["realtime_end"] <= "2026-12-31" and "as_of_after_provider_today_vintage_clamped" in result.warnings


def test_no_key_is_unavailable_with_provider_key_missing_FT_094() -> None:
    result = call(key=lambda p: None)
    assert (result.status, result.source, result.reason, result.value) == (sentinels.DATA_UNAVAILABLE, "fred", sentinels.REASON_KEY_MISSING, None)


def test_series_aliases_cover_the_named_indicators_FT_094() -> None:
    for alias, series in {"nonfarm_payrolls": "PAYEMS", "unemployment": "UNRATE", "retail_sales": "RSAFS", "industrial_production": "INDPRO",
                          "housing_starts": "HOUST", "personal_income": "PI", "mortgage_rate_30y": "MORTGAGE30US", "recession": "USREC",
                          "real_yield_10y": "DFII10", "breakeven_10y": "T10YIE", "CPI": "CPIAUCSL"}.items():
        assert resolve_series_id(alias) == series
    assert resolve_series_id("dgs10") == "DGS10" and resolve_series_id("Fed Funds") == "DFF"
    with pytest.raises(ValueError):
        resolve_series_id("bank of japan rate")
    phrase = call(series="bank of japan rate")
    assert phrase.status == sentinels.NO_DATA and phrase.reason == "unknown_series"


def test_unknown_series_and_empty_window_are_no_data_and_outages_are_unavailable_FT_094() -> None:
    assert call(transport=RecordedTransport(lambda u, p, h: (200, b'{"seriess": []}'))).status == sentinels.NO_DATA
    empty = lambda u, p, h: (200, fixture_bytes("fred_series_meta.json")) if u.endswith("/series") else (200, b'{"observations": []}')
    assert call(transport=RecordedTransport(empty)).reason == "no_observations_in_window"
    down = call(transport=RecordedTransport(constant(503)))
    assert (down.status, down.reason) == (sentinels.DATA_UNAVAILABLE, "provider_unreachable")
    limited = call(transport=RecordedTransport(constant(429)))
    assert limited.reason == "rate_limited" and limited.source == "fred"


def test_the_real_library_exposes_every_economy_getter_we_route_FT_095() -> None:
    names = _lib.public_methods("economics")
    assert {method for method in COUNTRY_KINDS.values()} <= names


class FakeEconomics:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs

    def get_consumer_price_index(self, **kwargs) -> pd.DataFrame:
        index = pd.period_range("2023-10", "2024-06", freq="M")
        return pd.DataFrame({"Germany": range(len(index)), "Japan": range(100, 100 + len(index))}, index=index)

    def get_central_bank_policy_rate(self, **kwargs):
        raise ValueError("Please enter a FRED API key")

    def get_money_supply(self, **kwargs):
        raise ConnectionError("no route")

    def get_gross_domestic_product(self, **kwargs) -> pd.DataFrame:
        return pd.DataFrame()


def test_other_countries_are_cut_at_as_of_FT_095() -> None:
    result = country_series("consumer_price_index", as_of=when("2024-02-15"), factory=FakeEconomics, key=key, clock=lambda: when("2026-01-01"))
    assert result.ok and result.source == "economics" and result.received_at
    months = [row["index"] for row in result.value]
    assert months[-1].startswith("2024-02") and all(m < "2024-03" for m in months)               # March to June are the future of that date
    assert "economy_series_are_latest_revision_not_vintage" in result.warnings
    early = country_series("consumer_price_index", as_of=when("2020-01-01"), factory=FakeEconomics, key=key)
    assert early.status == sentinels.NO_DATA and early.reason == "no_rows_by_as_of"


def test_other_countries_failures_become_sentinels_FT_095() -> None:
    nokey = country_series("central_bank_policy_rate", as_of=when("2024-02-15"), factory=FakeEconomics, key=lambda p: None)
    assert (nokey.status, nokey.reason) == (sentinels.DATA_UNAVAILABLE, sentinels.REASON_KEY_MISSING)
    down = country_series("money_supply", as_of=when("2024-02-15"), factory=FakeEconomics, key=key)
    assert (down.status, down.reason) == (sentinels.DATA_UNAVAILABLE, "provider_unreachable")
    assert country_series("gross_domestic_product", as_of=when("2024-02-15"), factory=FakeEconomics, key=key).status == sentinels.NO_DATA
    with pytest.raises(ValueError):
        country_series("nonsense", as_of=when("2024-02-15"), factory=FakeEconomics)


def test_the_real_library_exposes_every_fixed_income_getter_we_route_FT_096() -> None:
    names = _lib.public_methods("fixed_income")
    assert {method for method, _ in FIXED_INCOME_KINDS.values()} <= names


class FakeFixedIncome:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs

    def get_treasury_rates(self, **kwargs) -> pd.DataFrame:
        return pd.DataFrame({"10Y": [4.1, 4.2, 4.3]}, index=pd.DatetimeIndex(["2024-02-28", "2024-03-01", "2024-03-04"]))

    def get_duration(self, **kwargs) -> pd.DataFrame:
        return pd.DataFrame({"duration": [6.1]}, index=["BOND1"])

    def get_yield_to_maturity(self, **kwargs):
        return pd.DataFrame()


def test_fixed_income_series_are_cut_and_live_only_kinds_are_withheld_FT_096() -> None:
    kwargs = dict(factory=FakeFixedIncome, key=key, today=lambda: "2026-11-22", clock=lambda: when("2026-11-22T12:00:00"))
    curve = fixed_income("treasury_rates", as_of=when("2024-03-01"), **kwargs)
    assert curve.ok and curve.source == "fixed_income" and [r["index"][:10] for r in curve.value] == ["2024-02-28", "2024-03-01"]
    past_duration = fixed_income("duration", as_of=when("2024-03-01"), **kwargs)
    assert past_duration.status == sentinels.NO_DATA and past_duration.reason == sentinels.REASON_UNSUPPORTED and "no vintage" in past_duration.warnings[0]
    today_duration = fixed_income("duration", as_of=when("2026-11-22"), **kwargs)
    assert today_duration.ok and today_duration.value[0]["duration"] == 6.1
    assert fixed_income("yield_to_maturity", as_of=when("2026-11-22"), **kwargs).status == sentinels.NO_DATA
    assert fixed_income("treasury_rates", as_of=when("2020-01-01"), **kwargs).reason == "no_rows_by_as_of"
    with pytest.raises(ValueError):
        fixed_income("nonsense", as_of=when("2024-03-01"), **kwargs)


def test_fixed_income_passes_the_keys_it_has_FT_096() -> None:
    seen = {}

    class Spy(FakeFixedIncome):
        def __init__(self, **kwargs) -> None:
            seen.update(kwargs)

    fixed_income("treasury_rates", as_of=when("2024-03-01"), factory=Spy, key=lambda p: f"k-{p}")
    assert seen["fred_api_key"] == "k-fred" and seen["api_key"] == "k-fmp" and seen["end_date"] == "2024-03-01"


# ------------------------------------------------------------------------------------------------ OA-66
def board_fetch(series_id, *, as_of, look_back_days, clock, fail=()):
    if series_id in fail:
        return sentinels.unavailable("fred", "rate_limited", as_of=as_of)
    obs = [{"date": f"{y}-01-01", "value": float(100 + 5 * (y - 2022))} for y in (2022, 2023, 2024)]
    return sentinels.ok({"observations": obs, "latest": obs[-1], "vintage": "2024-03-01"}, source="fred", as_of=as_of)


def test_board_has_a_card_per_series_and_derives_cpi_yoy_OA_66() -> None:
    result = macro_board(as_of=when("2024-03-01"), fetch=board_fetch, clock=lambda: when("2026-01-01"))
    assert result.ok and result.source == "fred"
    ids = [c["id"] for c in result.value["cards"]]
    assert ids[:-1] == [s for s, _, _ in CURATED_BOARD] and ids[-1] == "CPI_YOY"
    assert result.value["cards"][-1]["latest"]["value"] == pytest.approx((110 / 105 - 1) * 100)


def test_a_failed_series_is_a_card_with_its_reason_and_all_failing_is_unavailable_OA_66() -> None:
    partial = macro_board(as_of=when("2024-03-01"), fetch=lambda s, **k: board_fetch(s, fail=("DGS10",), **k), clock=lambda: when("2026-01-01"))
    assert partial.ok and partial.value["failures"] == {"DGS10": {"status": sentinels.DATA_UNAVAILABLE, "reason": "rate_limited"}}
    assert "DGS10:rate_limited" in partial.warnings and "DGS10" not in [c["id"] for c in partial.value["cards"]]
    dead = macro_board(as_of=when("2024-03-01"), fetch=lambda s, **k: board_fetch(s, fail=[x for x, _, _ in CURATED_BOARD] + ["CPIAUCSL"], **k), clock=lambda: when("2026-01-01"))
    assert dead.status == sentinels.DATA_UNAVAILABLE and dead.reason == "rate_limited"


def test_year_over_year_skips_dates_without_a_year_ago_match_OA_66() -> None:
    obs = [{"date": "2023-01-01", "value": 100.0}, {"date": "2023-06-01", "value": 104.0}, {"date": "2024-01-01", "value": 110.0}, {"date": "2024-03-01", "value": 120.0}]
    assert year_over_year(obs) == [{"date": "2024-01-01", "value": pytest.approx(10.0)}]


def test_cached_board_serves_stale_when_the_upstream_fails_OA_66() -> None:
    state = {"now": when("2024-01-01T00:00:00")}
    cache = CachedBoard(60, clock=lambda: state["now"])
    value, meta = cache.get("k", lambda: {"v": 1})
    assert value == {"v": 1} and meta == {"cached_at": None, "stale": False}
    state["now"] = when("2024-01-01T00:00:30")
    hit, meta = cache.get("k", lambda: pytest.fail("fresh: no reload"))
    assert hit == {"v": 1} and meta["stale"] is False and meta["cached_at"] is not None
    state["now"] = when("2024-01-01T00:05:00")
    old, meta = cache.get("k", lambda: (_ for _ in ()).throw(RateLimitError("429")))
    assert old == {"v": 1} and meta["stale"] is True                                  # last good value, marked stale
    with pytest.raises(RateLimitError):
        CachedBoard(60).get("never", lambda: (_ for _ in ()).throw(RateLimitError("429")))   # nothing to serve: stays loud


def test_global_board_normalizes_units_and_never_copies_between_countries_OA_66() -> None:
    raw = {"cpi_yoy": [{"date": "2024-01", "country": "Japan", "value": 2.8}, {"date": "2024-02", "country": "Japan", "value": 2.9}],
           "short_rate": [{"date": "2024-02", "country": "Japan", "value": 0.0037}, {"date": "2024-02", "country": "Germany", "value": 0.0372}]}
    rows = {r["country"]: r for r in build_global_macro(raw, ["Japan", "Germany", "Brazil"])}
    assert rows["Japan"]["cpi_yoy"] == {"value": 2.9, "date": "2024-02"} and rows["Japan"]["short_rate"]["value"] == pytest.approx(0.37)
    assert rows["Germany"]["short_rate"]["value"] == pytest.approx(3.72) and rows["Germany"]["cpi_yoy"] == {"value": None, "date": None}
    assert rows["Brazil"]["short_rate"]["value"] is None and normalize_global_cell("cpi_yoy", 3.81) == 3.81


def test_the_module_is_callable_like_the_package_function() -> None:
    assert macro_module("CPIAUCSL", as_of=when("2024-03-01"), key=key, transport=RecordedTransport(fred_answer)).ok
    import hokma.data as data

    assert data.macro is macro_module
