"""Strategy files of the strategy ideas (SPEC-020: ECT-052 to ECT-055): each loads in the registry and builds its alpha with the declared parameters."""
import importlib.util
from pathlib import Path

import pytest

from hokma.factory.registry import Registry

ROOT = Path(__file__).resolve().parents[3]
FOLDER = ROOT / "kit" / "template" / "service" / "strategies"
_spec = importlib.util.spec_from_file_location("check_repo_rules", ROOT / "scripts" / "check_repo_rules.py")
repo_rules = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(repo_rules)
FILES = {"trend-channel.yaml": ["trend_channel_swc_fast", "trend_channel_swc_slow", "trend_regime_dt"],
         "mean-reversion.yaml": ["mean_reversion_dt", "mean_reversion_swc"],
         "momentum.yaml": ["momentum_swc", "momentum_lp_dual"],
         "pairs.yaml": ["pairs_spread_swc", "pairs_spread_lp"]}
LINES = {"trend_channel_swc_fast": "SWC", "trend_channel_swc_slow": "SWC", "trend_regime_dt": "DT", "mean_reversion_dt": "DT", "mean_reversion_swc": "SWC",
         "momentum_swc": "SWC", "momentum_lp_dual": "LP", "pairs_spread_swc": "SWC", "pairs_spread_lp": "LP"}


@pytest.mark.parametrize("name,ids", sorted(FILES.items()))
def test_the_file_loads_and_every_entry_builds_its_alpha(name, ids):
    registry = Registry.with_builtins()
    assert registry.load_file(FOLDER / name) == len(ids)
    for entry_id in ids:
        entry = registry.get(entry_id)
        assert entry.kind == "alpha" and entry.line == LINES[entry_id] and entry.description
        alpha = registry.build(entry_id)
        assert callable(alpha.signal) and set(alpha.FACTS) >= {"close"} | ({"high", "low"} if "trend" in entry_id else set())


@pytest.mark.parametrize("name", sorted(FILES))
def test_the_file_is_english_ascii_and_passes_the_repo_rules(name):
    path = FOLDER / name
    assert path.read_text(encoding="utf-8").isascii()
    assert repo_rules.check(ROOT, [path.relative_to(ROOT).as_posix()]) == []


def test_the_variants_differ_where_the_rows_say_they_do():
    registry = Registry.with_builtins()
    for name in FILES:
        registry.load_file(FOLDER / name)
    dt, swc = registry.build("mean_reversion_dt"), registry.build("mean_reversion_swc")
    assert (dt.unit, swc.unit) == ("session_minutes", "days")                                  # the half-life unit of each line
    assert registry.build("trend_regime_dt").adx_min > 0 and registry.build("trend_channel_swc_fast").adx_min == 0     # the intraday filter is the ADX alone
    assert registry.build("trend_channel_swc_fast").skip_after_winner and not registry.build("trend_channel_swc_slow").skip_after_winner
    assert (registry.build("trend_channel_swc_slow").entry_n, registry.build("trend_channel_swc_slow").exit_m) == (55, 20)
    lp = registry.build("momentum_lp_dual")
    assert lp.absolute_filter and lp.long_only and not registry.build("momentum_swc").absolute_filter and lp.vol_off_percentile == 90.0
    assert registry.build("pairs_spread_lp").hedge == "kalman" and registry.build("pairs_spread_swc").hedge == "ols"
