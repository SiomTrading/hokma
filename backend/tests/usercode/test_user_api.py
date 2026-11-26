"""user_api: the only module of Hokma that generated code sees (SPEC-031 section 5.3). It loads in a process that is not the server."""
import subprocess
import sys

import pytest

from hokma.factory import user_api
from hokma.factory.indicator_library import LIBRARY


def test_the_public_names_are_a_fixed_list_and_each_one_exists():
    assert set(user_api.__all__) == {
        "dataclass", "ClassVar", "Param", "ParamSchema", "Criterion", "Signal", "SignalContext", "RuleBarContext", "IndicatorRequest", "fact", "view",
        "clip", "Price", "Ticks", "ATR", "Pct", "INDICATORS", "ind",
        "Decision", "long", "short", "flat", "hold", "replay", "bars_from_closes", "build_strategy"}
    assert not [n for n in user_api.__all__ if not hasattr(user_api, n)]


def test_the_indicator_names_are_the_kinds_of_the_library_of_hokma():
    assert set(user_api.INDICATORS) == set(LIBRARY)


def test_it_loads_in_a_clean_process_that_is_not_the_server_and_pulls_nothing_sensitive():
    code = ("import sys\nimport hokma.factory.user_api\n"
            "bad = [m for m in sys.modules if m.startswith(('hokma.desks', 'hokma.db', 'hokma.exec', 'hokma.broker', 'hokma.kit', 'hokma.backtest', 'hokma.schwab', "
            "'hokma.connections', 'hokma.llm', 'hokma.market', 'hokma.mail', 'hokma.telegram')) or m in ('pandas', 'sqlite3', 'psycopg', 'socket', 'subprocess', 'ssl')]\n"
            "print(bad)\nassert not bad, bad")
    done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr


def test_a_view_clips_to_one_and_rejects_what_is_not_a_finite_number():
    ctx = user_api.SignalContext(source="u", symbol="NQ", as_of="2026-01-02T15:00:00+00:00", facts={})
    assert user_api.view(ctx, "u_x", 3.0, (), "r").value == 1.0
    assert user_api.view(ctx, "u_x", -3.0, (), "r").value == -1.0
    for bad in (float("nan"), float("inf"), "1", None, True):
        with pytest.raises(ValueError):
            user_api.view(ctx, "u_x", bad, (), "r")
    assert user_api.view(ctx, "u_x", 0.5, (), "r").abstained is False and ctx.abstain("no view", ("close",)).abstained is True


def test_fact_gives_a_clean_array_or_none_like_the_alphas_of_the_factory():
    import numpy as np

    ctx = user_api.SignalContext(source="u", symbol="NQ", as_of="x", facts={"a": [1.0, 2.0, 3.0], "b": [1.0, float("nan")], "c": "text", "d": [[1.0]]})
    assert list(user_api.fact(ctx, "a", 2)) == [1.0, 2.0, 3.0] and isinstance(user_api.fact(ctx, "a"), np.ndarray)
    assert user_api.fact(ctx, "a", 4) is None and user_api.fact(ctx, "b", 1) is None and user_api.fact(ctx, "c") is None
    assert user_api.fact(ctx, "d") is None and user_api.fact(ctx, "missing") is None


def test_the_units_are_validated_and_travel_as_data():
    assert user_api.Price(10.5).to_data() == {"type": "price", "value": 10.5}
    assert user_api.Ticks(12).to_data() == {"type": "ticks", "value": 12}
    assert user_api.ATR(1.5).to_data() == {"type": "atr", "value": 1.5}
    assert user_api.Pct(0.5).to_data() == {"type": "pct", "value": 0.5}
    for unit in (user_api.Price, user_api.Ticks, user_api.ATR, user_api.Pct):
        for bad in (0, -1, float("nan"), float("inf"), "3", None, True):
            with pytest.raises(ValueError):
                unit(bad)


def test_an_indicator_request_is_checked_against_the_library_names():
    request = user_api.IndicatorRequest("fast", "ema", "close", (("length", 9),), 9)
    assert request.to_data() == {"name": "fast", "kind": "ema", "source": "close", "params": [["length", 9]], "warmup": 9}
    for kind in ("os", "system", "", "EMA"):
        with pytest.raises(ValueError):
            user_api.IndicatorRequest("x", kind)


def test_the_helpers_of_ind_are_causal_numpy_functions():
    import numpy as np

    x = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    assert list(user_api.ind.sma(x, 2)[1:]) == [1.5, 2.5, 3.5, 4.5] and np.isnan(user_api.ind.sma(x, 2)[0])
    assert user_api.ind.highest(x, 3)[-1] == 5.0 and user_api.ind.lowest(x, 3)[-1] == 3.0
    ema = user_api.ind.ema(x, 3)
    assert np.isnan(ema[0]) and ema[2] == pytest.approx(2.0) and np.allclose(user_api.ind.ema(x[:4], 3), ema[:4], equal_nan=True)
    assert not [n for n in dir(user_api.ind) if n.startswith("_") and not n.startswith("__")]
