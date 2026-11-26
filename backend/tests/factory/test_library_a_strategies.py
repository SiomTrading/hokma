"""Strategy files of the library, block A (SPEC-020: AH-30, AH-31, AH-32, AS-006): schema, lines, flags and notices."""
import importlib.util
import re
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from hokma.desks.core.signal import SignalContext
from hokma.factory.alphas.premium_wheel import PremiumWheel
from hokma.factory.alphas.value_screen import CRITERIA_BY_ASSET_CLASS
from hokma.factory.registry import Registry

ROOT = Path(__file__).resolve().parents[3]
STRATEGIES = ROOT / "kit" / "template" / "service" / "strategies"
NOTICES = ROOT / ".notices" / "THIRD_PARTY.txt"
FILES = ("deep-value.yaml", "fundamental-long-short.yaml", "inflections.yaml", "premium-wheel.yaml", "intraday-bot-skeleton.yaml")
AS_OF = datetime(2025, 3, 3, 21, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def registry() -> Registry:
    reg = Registry.with_builtins()
    for name in FILES:
        reg.load_file(STRATEGIES / name)
    return reg


def entries(registry: Registry, prefix: str):
    return [e for e in registry.entries.values() if e.id.startswith(prefix)]


def ctx(line: str, **facts) -> SignalContext:
    return SignalContext("alpha.test", 7, line, "X", AS_OF, "days", "h" * 8, facts={k: np.asarray(v, dtype=float) for k, v in facts.items()})


def notice_refs() -> set[str]:
    return set(re.findall(r"^- (idea-\d{3}):", NOTICES.read_text(encoding="utf-8"), flags=re.M))


def test_every_file_loads_through_the_factory_loader(registry):
    assert len(registry.entries) == 6 + 14
    for name in FILES:
        assert Registry.with_builtins().load_file(STRATEGIES / name) >= 1
    assert all(e.kind == "alpha" and e.line is not None for e in registry.entries.values() if not e.source == "builtin")


def test_each_entry_points_to_a_notice_line_and_builds(registry):
    refs = notice_refs()
    for entry in registry.entries.values():
        if entry.source == "builtin":
            continue
        found = re.search(r" ref (idea-\d{3})$", entry.description)
        assert found and found.group(1) in refs, entry.id
        assert registry.build(entry.id) is not None


def test_deep_value_declares_its_lines_and_the_asset_classes_of_its_criteria(registry):
    rows = entries(registry, "deep_value")
    assert {e.line for e in rows} >= {"SWO", "LP"}
    by_class = {e.params["asset_class"]: CRITERIA_BY_ASSET_CLASS[e.params["asset_class"]] for e in rows}
    assert set(by_class) == {"equity", "etf", "future"}
    assert "fcf_yield" in by_class["equity"] and by_class["future"] == ("price_to_average",)
    for entry in rows:                                          # the facts the alpha reads are the ones declared for the class
        assert registry.build(entry.id).FACTS == CRITERIA_BY_ASSET_CLASS[entry.params["asset_class"]]
    swo = registry.build("deep_value_swo_equity")
    assert swo.signal(ctx("SWO", earnings_yield=[0.10], fcf_yield=[0.07], book_to_price=[0.5])).value == pytest.approx(2 / 3)


def test_fundamental_long_short_is_a_dollar_neutral_book_in_each_line(registry):
    rows = entries(registry, "fundamental_ls")
    assert {e.line for e in rows} >= {"SWO", "LP"}
    assert all(e.params["book"] == "dollar_neutral" for e in rows)
    future = registry.build("fundamental_ls_swc_future")
    assert future.weight_quality == 0.0                         # a future has no statements
    long_short = registry.build("fundamental_ls_lp")
    assert long_short.signal(ctx("LP", value_rank=[0.1], quality_rank=[0.2], momentum_rank=[0.3])).value == pytest.approx(-0.64)   # composite 0.04 + 0.08 + 0.06 = 0.18; 2 * (0.18 - 0.5)


def test_inflection_window_differs_by_line_and_futures_use_price_and_macro_only(registry):
    rows = entries(registry, "inflection")
    windows: dict[str, set[int]] = {}
    for entry in rows:
        windows.setdefault(entry.line, set()).add(entry.params["window"])
    assert windows == {"LP": {4}, "SWO": {2}, "SWC": {63, 3}}
    assert {e.params["source"] for e in rows if e.line == "SWC"} == {"price_roc", "macro_change"}
    assert {e.params["source"] for e in rows if e.line != "SWC"} == {"growth"}
    lp = registry.build("inflection_lp_growth")
    assert lp.signal(ctx("LP", growth=[0.02, 0.03, 0.04, 0.05, 0.07])).value == pytest.approx(1.0)


def test_premium_wheel_requires_the_premium_switch_and_cannot_be_declared_without_it(registry):
    entry = registry.get("premium_wheel_swo")
    assert entry.line == "SWO" and entry.params["requires_premium_switch"] is True
    assert entry.cls is PremiumWheel and entry.cls.REQUIRES_PREMIUM_SWITCH is True
    assert registry.build("premium_wheel_swo").requires_premium_switch is True
    with pytest.raises(ValueError, match="premium switch"):
        registry.build("premium_wheel_swo", {"requires_premium_switch": False})
    # the backtest of the recipe runs on facts like any alpha
    signal = registry.build("premium_wheel_swo").signal(ctx("SWO", iv_rv_ratio=[1.8], shares_held=[0.0]))
    assert signal.value == 1.0


def test_intraday_bot_skeleton_has_a_trend_leg_and_a_fade_leg_for_dt(registry):
    rows = entries(registry, "intraday_bot")
    assert sorted(e.id for e in rows) == ["intraday_bot_fade", "intraday_bot_trend"] and {e.line for e in rows} == {"DT"}
    trend, fade = registry.build("intraday_bot_trend"), registry.build("intraday_bot_fade")
    assert trend.signal(ctx("DT", ema_fast=[100.1], ema_slow=[100.0])).value == pytest.approx(0.5)      # 0.001 over scale 0.002
    assert fade.signal(ctx("DT", rsi=[10.0])).value == pytest.approx(0.5)                                # (20 - 10) / 20


def test_files_follow_the_repository_rules():
    spec = importlib.util.spec_from_file_location("check_repo_rules", ROOT / "scripts" / "check_repo_rules.py")
    rules = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rules)
    for name in FILES:
        text = (STRATEGIES / name).read_text(encoding="utf-8")
        assert not rules.origin_hits(text) and not rules.spanish_hits(text) and not rules.engine_hits(text), name
        assert "\u2014" not in text
