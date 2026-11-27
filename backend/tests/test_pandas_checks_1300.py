"""1.3.0 (task 3433), A: the dry run of a strategy in pandas. It runs, it decides, it is deterministic and it does not look at the future, and each failure says the rule, the line or the field and the change."""
import numpy as np
import pytest

from hokma.factory.pandas_strategy import checks, contract, sessions

PARAMS_LINE = 'PARAMS = {"ema_n": {"default": 9, "min": 3, "max": 30, "step": 1, "type": "int"}, "atr_n": {"default": 14, "min": 5, "max": 40, "step": 1, "type": "int"}, "atr_mult": {"default": 2.0, "min": 1.0, "max": 4.0, "step": 0.5, "type": "float"}}'
REFERENCE = f'''import pandas as pd
import numpy as np

{PARAMS_LINE}


def atr(df, n):
    prev = df.close.shift(1)
    tr = pd.concat([df.high - df.low, (df.high - prev).abs(), (df.low - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / n, adjust=False).mean()


def generate_signals(df, params):
    ema = df["close"].ewm(span=params["ema_n"], adjust=False).mean()
    typical = (df.high + df.low + df.close) / 3.0
    anchor = (df.is_rth & ~df.is_rth.shift(1, fill_value=False)).cumsum()
    pv = (typical * df.volume).where(df.is_rth, 0.0).groupby(anchor).cumsum()
    vv = df.volume.where(df.is_rth, 0.0).groupby(anchor).cumsum()
    vwap = pv / vv.where(vv > 0)
    a = atr(df, params["atr_n"])
    cross_up = (ema > vwap) & (ema.shift(1) <= vwap.shift(1))
    cross_dn = (ema < vwap) & (ema.shift(1) >= vwap.shift(1))
    out = pd.DataFrame(index=df.index)
    out["entry_long"] = cross_up & df.is_rth
    out["entry_short"] = cross_dn & df.is_rth
    out["exit_long"] = df.close < ema
    out["exit_short"] = df.close > ema
    out["stop"] = np.where(out.entry_long, df.close - params["atr_mult"] * a, np.where(out.entry_short, df.close + params["atr_mult"] * a, np.nan))
    return out
'''
OPENING_RANGE = '''import pandas as pd
import numpy as np

PARAMS = {"bars": {"default": 6, "min": 2, "max": 12, "step": 1, "type": "int"}, "risk": {"default": 15.0, "min": 5.0, "max": 40.0, "step": 5.0, "type": "float"}}


def generate_signals(df, params):
    k = df.is_rth.astype(int).groupby(df.session_id).cumsum()
    in_range = df.is_rth & (k <= params["bars"])
    high = df.high.where(in_range).groupby(df.session_id).cummax().groupby(df.session_id).ffill()
    low = df.low.where(in_range).groupby(df.session_id).cummin().groupby(df.session_id).ffill()
    after = df.is_rth & (k > params["bars"])
    out = pd.DataFrame(index=df.index)
    out["entry_long"] = after & (df.close > high.shift(1))
    out["entry_short"] = after & (df.close < low.shift(1))
    out["exit_long"] = ~df.is_rth
    out["exit_short"] = ~df.is_rth
    out["stop"] = np.where(out.entry_long, df.close - params["risk"], np.where(out.entry_short, df.close + params["risk"], np.nan))
    return out
'''
RSI_RESET = '''import pandas as pd
import numpy as np

PARAMS = {"n": {"default": 7, "min": 3, "max": 20, "step": 1, "type": "int"}, "low": {"default": 25.0, "min": 10.0, "max": 40.0, "step": 5.0, "type": "float"}}


def rsi(s, n):
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1.0 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1.0 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn)


def generate_signals(df, params):
    r = rsi(df.close, params["n"])
    fresh = r.shift(1) >= params["low"]
    out = pd.DataFrame(index=df.index)
    out["entry_long"] = (r < params["low"]) & fresh & df.is_rth
    out["entry_short"] = (r > 100 - params["low"]) & (r.shift(1) <= 100 - params["low"]) & df.is_rth
    out["exit_long"] = r > 50
    out["exit_short"] = r < 50
    out["stop"] = np.where(out.entry_long, df.low.rolling(10, min_periods=1).min() - 1.0, np.where(out.entry_short, df.high.rolling(10, min_periods=1).max() + 1.0, np.nan))
    out["target"] = np.where(out.entry_long, df.close + 30.0, np.where(out.entry_short, df.close - 30.0, np.nan))
    return out
'''


def rules(source, **kw):
    result = checks.dry_run(source, **kw)
    return result, [p.rule for p in result.problems]


# ----------------------------------------------------------------------------------------------------------------------------- the sample
def test_the_sample_is_the_same_every_time_and_has_every_case_of_session_and_regular_hours():
    a, b = checks.sample_bars(), checks.sample_bars()
    assert np.array_equal(a[0], b[0]) and all(np.array_equal(a[1][k], b[1][k]) for k in a[1])
    times, cols = a
    session, rth = sessions.session_columns(times, 300)
    assert len(np.unique(session)) == checks.SESSIONS and rth.any() and (~rth).any() and np.all(np.diff(times) > 0)
    assert np.all(cols["high"] >= np.maximum(cols["open"], cols["close"])) and np.all(cols["low"] <= np.minimum(cols["open"], cols["close"]))
    gaps = np.diff(times) > 300 * 10**9 * 1.5
    assert gaps.sum() >= 9                                                                   # the break of every session and the weekend between the two weeks
    assert len(times) == checks.SESSIONS * 23 * 12 and times[0] == 1_748_815_200 * 10**9          # Sunday 1 June 2025, 18:00 New York


def test_the_cuts_are_30_to_50_at_random_plus_every_change_of_session_and_of_regular_hours():
    times, _ = checks.sample_bars()
    cuts = checks._cut_points(times, 300, np.random.default_rng(5))
    session, rth = sessions.session_columns(times, 300)
    change = np.flatnonzero(np.r_[False, session[1:] != session[:-1]])
    flip = np.flatnonzero(np.r_[False, rth[1:] != rth[:-1]])
    assert 30 <= len(cuts) <= 50 + 2 * (12 + 12)
    for edge in (*change[:12], *flip[:12]):
        assert edge - 1 in cuts and edge in cuts


# ----------------------------------------------------------------------------------------------------------------------------- a good strategy passes, the usual ones too
@pytest.mark.parametrize("source", [REFERENCE, OPENING_RANGE, RSI_RESET], ids=["ema cross vwap rth", "opening range", "rsi with target"])
def test_a_legal_strategy_passes_with_no_false_problem(source):
    result = checks.dry_run(source)
    assert result.ok, [p.text() for p in result.problems]
    assert result.stats["entries_long"] + result.stats["entries_short"] > 0 and result.stats["corner_min"] and result.stats["corner_max"] and 30 <= result.stats["cuts"]
    assert result.seconds < 20 and [d.name for d in result.defs]


# ----------------------------------------------------------------------------------------------------------------------------- each failure names its rule
@pytest.mark.parametrize("old, new", [
    ('df["close"].ewm(span=params["ema_n"], adjust=False).mean()', 'df["close"].ewm(span=params["ema_n"], adjust=False).mean().shift(-1)'),
    ('df["close"].ewm(span=params["ema_n"], adjust=False).mean()', 'df["close"].rolling(params["ema_n"], center=True).mean()'),
    ('df["close"].ewm(span=params["ema_n"], adjust=False).mean()', 'df["close"].bfill().ewm(span=params["ema_n"], adjust=False).mean() + df["close"].shift(-2).fillna(0) * 0 + df["close"].where(df["close"].index < df["close"].index[-1]).bfill() * 0'),
    ("a = atr(df, params[\"atr_n\"])", "a = atr(df, params[\"atr_n\"]) * 0 + df.close.std() * 0.001"),
    ("vwap = pv / vv.where(vv > 0)", "vwap = pv.groupby(anchor).transform('max') / vv.where(vv > 0)"),
])
def test_a_signal_that_uses_the_bars_after_is_caught_by_the_cuts_with_the_suspect_line(old, new):
    source = REFERENCE.replace(old.replace('\\"', '"'), new.replace('\\"', '"'))
    assert source != REFERENCE
    result, found = rules(source)
    assert found == ["looks_at_future"], found
    problem = result.problems[0]
    assert "cut after it" in problem.message and problem.field in contract.SIGNAL_COLUMNS and "shift(1)" in problem.fix


def test_the_suspects_are_lines_not_noise_and_the_reference_has_none():
    assert checks.suspects(REFERENCE) == []
    assert [n for n, _ in checks.suspects("x = df.close.shift(-1)\ny = df.close.rolling(5).mean()\nz = df.close.std()\nw = df.close.iloc[-1]\n")] == [1, 3, 4]


def test_a_strategy_that_never_enters_is_named():
    result, found = rules(REFERENCE.replace("cross_up & df.is_rth", "cross_up & (df.close < 0)").replace("cross_dn & df.is_rth", "cross_dn & (df.close < 0)"))
    assert found == ["no_decision"] and result.stats["stage"] == "decide" and "Loosen" in result.problems[0].fix


def test_a_strategy_that_changes_between_runs_is_named():
    source = REFERENCE.replace("    out = pd.DataFrame(index=df.index)\n", "    out = pd.DataFrame(index=df.index)\n    pick = df.close.sample(n=40).index\n").replace('out["exit_long"] = df.close < ema', 'out["exit_long"] = (df.close < ema) | df.index.isin(pick)').replace('out["entry_long"] = cross_up & df.is_rth', 'out["entry_long"] = cross_up & df.is_rth')
    result, found = rules(source)
    assert "not_deterministic" in found


def test_an_error_of_the_strategy_has_its_rule_its_line_and_its_text():
    source = REFERENCE.replace("    a = atr(df, params[\"atr_n\"])", "    a = atr(df, params[\"atr_n\"])\n    boom = df['nope']")
    result, found = rules(source)
    (problem,) = result.problems
    assert found == ["raised"] and problem.line == 21 and "KeyError" in problem.message and result.stats["stage"] == "run"


def test_a_failure_only_at_the_corner_of_params_says_which_corner():
    source = REFERENCE.replace('"ema_n": {"default": 9, "min": 3,', '"ema_n": {"default": 9, "min": 3,').replace("ema = df[\"close\"]", "bad = 1 / (params['ema_n'] - 3)\n    ema = df[\"close\"]")
    result, found = rules(source)
    assert found == ["raised"] and "with every parameter at its minimum" in result.problems[0].message


def test_a_strategy_that_fails_on_a_short_table_is_named_at_the_cut():
    source = REFERENCE.replace("    ema = df[\"close\"]", "    first = df.close.iloc[200]\n    ema = df[\"close\"]")
    result, found = rules(source)
    assert found == ["fails_on_short_table"] and "IndexError" in result.problems[0].message


def test_a_bad_answer_is_the_rule_of_the_format():
    result, found = rules(REFERENCE.replace('out["stop"] = np.where', 'out["unused"] = np.where'))
    assert "signals_columns" in found
    result, found = rules(REFERENCE.replace("df.close - params[\"atr_mult\"] * a", "df.close + params[\"atr_mult\"] * a"))
    assert found == ["signals_stop_side"] and result.problems[0].field == "stop"
    result, found = rules(REFERENCE.replace("    return out\n", "    return out.iloc[:100]\n"))
    assert found == ["bad_return"]


def test_the_format_comes_first_and_nothing_runs_for_a_text_without_it():
    result = checks.dry_run(REFERENCE.replace("def generate_signals(df, params):", "def other(df, params):"))
    assert not result.ok and result.stats["stage"] == "format" and result.problems[0].rule == "generate_signals"


def test_the_code_runs_as_any_python_with_no_list_of_what_it_may_say():
    result = checks.dry_run(REFERENCE.replace("import numpy as np", "import numpy as np\nimport os\nimport statistics"))
    assert result.ok, [p.text() for p in result.problems]
    assert "isolation" not in result.to_data() and "layers" not in result.stats


def test_an_error_of_the_strategy_is_a_problem_with_its_line():
    result = checks.dry_run(REFERENCE.replace("    ema = df[\"close\"]", "    raise ValueError(\"boom\")\n    ema = df[\"close\"]"))
    assert not result.ok and result.problems[0].rule == "raised" and "boom" in result.problems[0].message and result.problems[0].line
