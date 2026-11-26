"""The wiring of the factory library (SPEC-032: OPT-17): the first nine functions give the same bytes as before, and every function made from
the H3 indicators passes the causality rule of the strategy contract on the real bars of NQ."""
import json
from pathlib import Path

import numpy as np
import pytest
from factory import wiring_support as ws

import hokma.quant.indicators  # noqa: F401  (importing the package registers the indicators)
from hokma.factory import indicator_library as lib
from hokma.factory.strategy import BadStrategy
from hokma.quant import registry

BASELINE = Path(__file__).resolve().parent / "fixtures" / "indicator_library_baseline.json"
FIRST = ("ema", "sma", "rsi", "macd_line", "macd_signal", "adx", "atr", "half_life", "ewma_vol")


@pytest.fixture(scope="module")
def bars():
    return ws.nq_day()


@pytest.fixture(scope="module")
def probes(bars):
    return {h: ws.probe(h, bars) for h in ws.all_h3()}


def _first_nine(bars):
    c = bars["close"]
    return {"ema": lambda: lib.ema(c, 20), "sma": lambda: lib.sma(c, 20), "rsi": lambda: lib.rsi(c, 14), "macd_line": lambda: lib.macd_line(c, 12, 26),
            "macd_signal": lambda: lib.macd_signal(c, 12, 26, 9), "adx": lambda: lib.adx(bars, 14), "atr": lambda: lib.atr(bars, 14),
            "half_life": lambda: lib.half_life(c, 30), "ewma_vol": lambda: lib.ewma_vol(c, 94)}


def test_the_first_nine_give_the_same_bytes_as_before_the_wiring(bars):
    """The baseline file holds what the nine gave on the 390 bars of NQ before OPT-17 (captured from the code of that day)."""
    saved = json.loads(BASELINE.read_text(encoding="utf-8"))["functions"]
    calls = _first_nine(bars)
    assert set(saved) == set(calls) == set(FIRST)
    for name, call in calls.items():
        got = np.asarray(call(), dtype="<f8")
        assert len(got) == saved[name]["n"] and int(np.isfinite(got).sum()) == saved[name]["finite"] > 0, name
        assert got.tobytes() == bytes.fromhex(saved[name]["hex"]), name
        assert lib.LIBRARY[name] is getattr(lib, name)                      # the library still hands out the same function object


def test_the_library_keeps_its_nine_names_first_and_bar_kinds_grow_only_with_several_fields():
    assert tuple(lib.LIBRARY)[:9] == FIRST
    assert {"adx", "atr"} <= lib.BAR_KINDS
    several = {w.name for w in lib.WIRING if len(w.fields) > 1}
    assert lib.BAR_KINDS == {"adx", "atr"} | several
    assert set(lib.LIBRARY) == set(FIRST) | {w.name for w in lib.WIRING} and len(lib.WIRING) > 0
    assert not {w.name for w in lib.WIRING} & set(FIRST)


def test_a_row_of_the_wiring_names_a_real_h3_function_a_real_column_and_real_parameters():
    seen = set()
    for w in lib.WIRING:
        spec = registry.describe(w.h3)
        assert w.name not in seen and (w.h3.removeprefix("indicators."), w.column) not in ws.ORIGINALS
        seen.add(w.name)
        assert set(w.params.values()) <= set(spec.inputs), w.name
        assert all(spec.inputs[h3p]["type"] == "int" for h3p in w.params.values()), w.name          # the requests of an alpha carry whole numbers
        assert set(w.fields) <= set(ws.FIELDS) and w.fields, w.name


def test_every_row_of_the_wiring_passes_the_causality_rule_on_the_real_bars(probes):
    """The value of a bar does not change when the bars after it are removed (the rule of strategy._check_causal), column by column."""
    for w in lib.WIRING:
        probe = probes[w.h3]
        assert not probe.reason, (w.name, probe.reason)
        assert probe.columns.get(w.column) == "", (w.name, probe.columns.get(w.column))
        assert tuple(w.fields) == probe.fields, (w.name, w.fields, probe.fields)          # the fields are the ones H3 needs, no more
        assert set(w.params.values()) == set(probe.int_params), w.name


def test_the_functions_made_from_the_wiring_give_what_h3_gives(bars):
    """The function of each row is the column of the H3 function: the default case and one whole number parameter moved."""
    for w in lib.WIRING:
        spec = registry.describe(w.h3)
        source = bars["close"] if len(w.fields) == 1 else {k: bars[k] for k in w.fields}
        frame = ws.frame(bars, w.fields)
        base = np.asarray(spec.fn(frame, last_n=None).value["values"][w.column], dtype=float)       # type: ignore[misc]
        got = lib.LIBRARY[w.name](source)
        assert got.shape == base.shape == (len(bars["close"]),) and np.array_equal(got, base, equal_nan=True), w.name
        if w.params:
            lib_param, h3_param = next(iter(w.params.items()))
            moved = int(spec.inputs[h3_param]["default"]) + 1
            want = np.asarray(spec.fn(frame, last_n=None, **{h3_param: moved}).value["values"][w.column], dtype=float)    # type: ignore[misc]
            assert np.array_equal(lib.LIBRARY[w.name](source, **{lib_param: moved}), want, equal_nan=True), w.name


def test_a_wired_function_refuses_what_it_cannot_read(bars):
    with pytest.raises(BadStrategy, match="unknown parameter"):
        lib.LIBRARY["bollinger_upper"](bars["close"], period=20)
    with pytest.raises(BadStrategy, match="mapping of bar arrays"):
        lib.LIBRARY["cci"](bars["close"], length=14)
    with pytest.raises(BadStrategy, match="needs the bar field"):
        lib.LIBRARY["cci"]({"close": bars["close"]}, length=14)
    with pytest.raises(BadStrategy):
        lib.LIBRARY["bollinger_upper"](bars["close"], length=-3)                  # a bad parameter is an error, never a column of NaN
    assert np.isnan(lib.LIBRARY["bollinger_upper"](bars["close"][:5], length=20)).all()      # fewer bars than the window: the warm-up


def test_what_does_not_pass_is_listed_with_its_reason(probes, capsys):
    """H3 has more functions than the library takes: the ones that read a series, a table of symbols or only the last bar, or that look ahead,
    stay h3_only. The list is printed (pytest -s) and every entry has a reason."""
    wired = {(w.h3, w.column) for w in lib.WIRING}
    out: list[tuple[str, str]] = []
    for h3, probe in probes.items():
        if probe.reason:
            out.append((h3, probe.reason))
            continue
        out += [(f"{h3}.{c}", why) for c, why in probe.columns.items() if why]
    assert out and all(reason for _, reason in out)
    assert not [name for name, _ in out if any(name == f"{h}.{c}" for h, c in wired)]            # nothing wired is in the list
    with capsys.disabled():
        print(f"\nH3 indicators wired: {len(lib.WIRING)} columns; kept h3_only: {len(out)}")
        for name, reason in out:
            print(f"  h3_only  {name}: {reason}")
