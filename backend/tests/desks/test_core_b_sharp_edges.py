"""ED-037: sharp edges: patterns find known bad fragments written by an agent and let the correct ones pass."""
import pytest

from hokma.desks.core import sharp_edges as se

BAD = [
    ("df['signal'] = df['close'].shift(-1) > df['close']", "lookahead-negative-shift"),
    ("future = prices.pct_change(periods=-5)", "lookahead-negative-shift"),
    ("x = np.roll(closes, -1)", "lookahead-roll"),
    ("df = df.bfill()", "lookahead-backfill"),
    ("df['ma'] = df['close'].rolling(20, center=True).mean()", "lookahead-centered-window"),
    ("last_close = bars['close'].iloc[-1]", "open-candle"),
    ("sharpe = returns.mean() / returns.std()", "divide-by-zero-ratio"),
    ("ratio = gains.sum() / losses.sum()", "divide-by-zero-ratio"),
    ("pos = (close - low) / (high - low)", "divide-by-difference"),
    ("ann = daily.mean() * 365", "annualize-365"),
    ("vol = daily.std() * np.sqrt(365)", "annualize-365"),
    ("x_train, x_test = train_test_split(x, y, test_size=0.2)", "shuffled-time-split"),
    ("x_train, x_test = train_test_split(x, y, shuffle=True)", "shuffled-time-split"),
    ("result = eval(agent_text)", "dynamic-code"),
    ("os.system(command)", "dynamic-code"),
    ("subprocess.run(cmd, shell=True)", "dynamic-code"),
    ("if close == 100.25: buy()", "float-price-equality"),
]
GOOD = [
    "df['signal'] = df['close'].shift(1) > df['close']",
    "ret = prices.pct_change(5)",
    "df = df.ffill()",
    "df['ma'] = df['close'].rolling(20).mean()",
    "last_closed = bars['close'].iloc[-2]",
    "sharpe = safe_div(returns.mean(), returns.std())",
    "sharpe = returns.mean() / returns.std() if returns.std() else None",
    "ratio = gains.sum() / max(losses.sum(), 1e-9)",
    "pos = (close - low) / np.where(high > low, high - low, np.nan)",
    "ann = daily.mean() * 252",
    "x_train, x_test = train_test_split(x, y, shuffle=False)",
    "result = evaluate(agent_text)",
    "average = total / len(values)",
    "if close >= level: buy()",
    "# df['x'] = df['close'].shift(-1)  never do this",
    "message = 'use df.shift(-1) with care'  # sharp-edge: ok",
    "answer = model.retrieval(query)",
]


@pytest.mark.parametrize("code,rule", BAD)
def test_known_bad_fragments_are_detected_ED_037(code, rule):
    assert [f.rule_id for f in se.scan(code)] == [rule]


@pytest.mark.parametrize("code", GOOD)
def test_correct_fragments_pass_ED_037(code):
    assert se.scan(code) == []


def test_findings_say_where_why_and_how_to_fix_ED_037():
    code = "import pandas as pd\n\ndf = pd.read_csv('x.csv')\ndf['y'] = df['close'].shift(-1)\nlast = bars.iloc[-1]\n"
    findings = se.scan(code)
    assert [(f.line, f.rule_id) for f in findings] == [(4, "lookahead-negative-shift"), (5, "open-candle")]
    assert findings[0].severity == "high" and findings[0].fix and findings[0].summary and findings[0].text.startswith("df['y']")
    text = se.report(findings)
    assert "line 4 [high] lookahead-negative-shift" in text and "fix:" in text and se.report([]) == ""


def test_blockers_follow_the_severity_ED_037():
    low = se.scan("if price == 100.25: pass")
    assert low and not se.has_blockers(low) and se.has_blockers(low, at_least="low")
    assert se.has_blockers(se.scan("x = s.shift(-2)")) and not se.has_blockers([])


def test_a_hash_inside_a_string_is_still_code_ED_037():
    assert [f.rule_id for f in se.scan("label = '#'; y = s.shift(-1)")] == ["lookahead-negative-shift"]


def test_every_rule_has_a_unique_id_a_fix_and_a_known_severity_ED_037():
    ids = [r.id for r in se.RULES]
    assert len(set(ids)) == len(ids) == len(se.BY_ID)
    assert all(r.fix and r.summary and r.severity in se.SEVERITIES for r in se.RULES)


def test_a_custom_rule_set_is_honoured_ED_037():
    only = [se.BY_ID["dynamic-code"]]
    assert se.scan("y = s.shift(-1)\nz = eval(x)", only)[0].line == 2 and len(se.scan("y = s.shift(-1)\nz = eval(x)", only)) == 1
