"""The switch-off of the engine statistics (SPEC-020 section 4.8) and the answer `engine_unavailable` (N-27).

The analyzer of the engine copies its whole history on every closed position, so its cost grows with the square of the trades
(measured in the fiche of the engines: 502 s against 21 s on 12,219 trades) and Hokma never reads its numbers. `_lib.analyzer_off`
replaces the one method that does it, in memory, while the runs of the block go on. These tests hold three things: the result is the
same with and without the switch, the original method is back afterwards, and when the hook is not there the backtest does not start."""
from decimal import Decimal as D

import pytest

from backtest.support import MULT, TICK, bars_of, requires_engine, scenario_run, spec
from hokma import backtest
from hokma.backtest import _lib, engine
from hokma.backtest.fills import Decision
from hokma.backtest.spec import FillRules


def run(analyzer: bool, name: str = "T2", watch: list | None = None):
    bars = bars_of(name)

    def decide(i, bar, side):
        if watch is not None:
            watch.append(_lib.hook_active())
        return [Decision("enter", "long", stop_points=D(5), target_points=D(5))] if i == 1 else []

    return _lib.run_bars(bars=bars, instrument=_lib.InstrumentSpec("NQZ5", TICK, MULT), decide=decide, rules=FillRules(),
                         starting_balance=D(1_000_000), per_contract_fee=D("2.25"), analyzer=analyzer)


@requires_engine
def test_the_result_is_identical_with_and_without_the_switch_off():
    on, off = run(analyzer=True), run(analyzer=False)
    assert off.fills == on.fills and off.balance_delta == on.balance_delta == D("-114.50")
    assert off.ambiguous_bars == on.ambiguous_bars == 1 and off.engine_version == on.engine_version


@requires_engine
def test_the_hook_is_replaced_only_while_the_run_goes_on_and_restored_after():
    original = _lib.PortfolioAnalyzer.__dict__[_lib.ANALYZER_HOOK]
    assert _lib.hook_present() and not _lib.hook_active()
    inside_off, inside_on = [], []
    run(analyzer=False, watch=inside_off)
    run(analyzer=True, watch=inside_on)
    assert inside_off and all(inside_off) and inside_on and not any(inside_on)
    assert _lib.PortfolioAnalyzer.__dict__[_lib.ANALYZER_HOOK] is original and not _lib.hook_active()


@requires_engine
def test_the_switch_off_nests_and_restores_the_original_method_once():
    original = _lib.PortfolioAnalyzer.__dict__[_lib.ANALYZER_HOOK]
    with _lib.analyzer_off():
        with _lib.analyzer_off():
            assert _lib.hook_active()
        assert _lib.hook_active()
    assert _lib.PortfolioAnalyzer.__dict__[_lib.ANALYZER_HOOK] is original
    with pytest.raises(RuntimeError):
        with _lib.analyzer_off():
            raise RuntimeError("a failing run still restores the method")
    assert _lib.PortfolioAnalyzer.__dict__[_lib.ANALYZER_HOOK] is original


@requires_engine
def test_when_the_hook_is_missing_the_backtest_does_not_start(monkeypatch):
    monkeypatch.delattr(_lib.PortfolioAnalyzer, _lib.ANALYZER_HOOK)
    assert not _lib.hook_present() and engine.status() == {"available": True, "code": None, "engine_version": _lib.engine_version(), "analyzer_hook": False}
    with pytest.raises(_lib.EngineUnavailable) as err:
        scenario_run("T1")
    assert err.value.code == "engine_unavailable" and _lib.ANALYZER_HOOK in str(err.value)
    with pytest.raises(_lib.EngineUnavailable):
        run(analyzer=False)


def test_every_public_entry_answers_engine_unavailable_when_the_engine_cannot_be_imported(monkeypatch):
    monkeypatch.setattr(_lib, "_IMPORT_ERROR", ImportError("no wheel for this platform"))
    assert not _lib.available() and not _lib.hook_present() and not _lib.hook_active()
    assert engine.status() == {"available": False, "code": "engine_unavailable", "engine_version": None, "analyzer_hook": False}
    assert engine.status()["code"] == "engine_unavailable"
    bars = bars_of("T1")
    for call in (lambda: backtest.run(spec()),
                 lambda: engine.run(spec(), bars, lambda i, b, s: [], tick_size=TICK, multiplier=MULT, symbol="NQZ5", protective_orders=False),
                 lambda: _lib.engine_version(), lambda: _lib.analyzer_off().__enter__()):
        with pytest.raises(_lib.EngineUnavailable) as err:
            call()
        assert err.value.code == "engine_unavailable" and "no wheel" not in str(err.value) and "ImportError" in str(err.value)
