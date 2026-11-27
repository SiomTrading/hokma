"""1.3.0 (task 3433), flow: the strategy as text. strategy_dry_run and strategy_create are the whole road of the creator: no path, no Write, no calls of paperwork."""
import json

from code_support import rows
from code_support import db, env, pg_admin_url, pg_template, pg_url_migrated  # noqa: F401  (fixtures)
from e2e_h4.support import caller, call, failed, ok
from test_pandas_checks_1300 import REFERENCE

from hokma.core.config import hokma_home
from hokma.factory import creation_metrics
from hokma.factory.spec import default_registry, live_registry

CREATOR = "strategy-creator"
SPEC = """## What it trades
NQ, 5 minute bars, regular session only.

## Entry
Long when the EMA(9) of the close crosses above the VWAP anchored at the regular open; short when it crosses below.

## Exit and stop
Exit when the close is on the other side of the EMA(9). Stop at 2 ATR from the close of the signal bar, as a price.
"""
ARGS = {"title": "EMA9 crosses VWAP RTH", "summary": "EMA of 9 crossing the VWAP of the regular session, exit by close against the EMA, ATR stop.", "spec": SPEC, "code": REFERENCE, "market": "NQ", "timeframe": "5m"}


def who(conversation=11):
    return caller(CREATOR, conversation=conversation)


def test_the_happy_path_is_two_calls_and_all_the_paperwork_is_done_by_the_code(db):
    creation_metrics.reset()
    dry = ok("strategy_dry_run", {"code": REFERENCE}, who())
    assert dry["ok"] is True and dry["problems"] == [] and dry["stats"]["cuts"] >= 30 and [p["name"] for p in dry["params"]] == ["ema_n", "atr_n", "atr_mult"]
    made = ok("strategy_create", ARGS, who())
    assert made["created"] is True and made["code"] == "STR-001" and made["status"] == "generated" and made["registry_id"] == "u_str001_v1"
    folder = hokma_home() / made["folder"]
    for name in ("spec.md", "checklist.md", "plan.md", "tasks.md", "versions/v1/strategy.py"):
        assert (folder / name).is_file(), name
    assert (folder / "adr").is_dir() and (folder / "results").is_dir()
    assert "Long when the EMA(9)" in (folder / "spec.md").read_text(encoding="utf-8") and "| v1 |" in (folder / "spec.md").read_text(encoding="utf-8")
    assert (folder / "versions/v1/strategy.py").read_text(encoding="utf-8").startswith("import pandas as pd")
    # the card, the requirement, the version, the link with its spec
    assert rows(db, "SELECT status, line FROM strategy_bank")[0]["status"] == "draft"
    assert rows(db, "SELECT status, created_by FROM user_rule_versions")[0] == {"status": "generated", "created_by": CREATOR}
    assert len(rows(db, "SELECT 1 FROM user_rule_briefs WHERE status = 'current'")) == 1 and len(rows(db, "SELECT 1 FROM strategy_spec_rules")) == 1
    assert rows(db, "SELECT reviewer FROM user_rule_reviews")[0]["reviewer"] == "hokma-checks"
    # the tasks and the journal
    titles = [t["title"] for t in rows(db, "SELECT title FROM tasks ORDER BY id")]
    assert len(titles) == 4 and "(Ben)" in titles[2] and rows(db, "SELECT count(*) AS n FROM task_links")[0]["n"] >= 3
    assert rows(db, "SELECT status FROM tasks ORDER BY id")[0]["status"] == "done" and rows(db, "SELECT status FROM tasks ORDER BY id")[2]["status"] == "ready"
    assert len(rows(db, "SELECT 1 FROM journal_entries")) == 1
    # available to the backtest and the optimizer at once, not to paper or real money
    assert made["registry_id"] in default_registry().entries and made["registry_id"] not in live_registry().entries
    # the measure: two calls to tools (one dry run, one create), three turns of the model, against 75 of the baseline
    measure = made["measure"]
    assert (measure["tool_calls"], measure["llm_calls"], measure["dry_runs"]) == (2, 3, 1) and measure["baseline"][0]["llm_calls"] == 75 and "Baseline 1.2.8 (Sam)" in measure["summary"]
    assert len(rows(db, "SELECT 1 FROM creation_metrics WHERE kind = 'creation'")) == 1


def test_one_retry_of_the_dry_run_after_a_problem_is_still_under_the_goal(db):
    creation_metrics.reset()
    leaky = REFERENCE.replace('df["close"].ewm(span=params["ema_n"], adjust=False).mean()', 'df["close"].ewm(span=params["ema_n"], adjust=False).mean().shift(-1)')
    first = ok("strategy_dry_run", {"code": leaky}, who(12))
    assert first["ok"] is False and first["problems"][0]["rule"] == "looks_at_future" and "fix" in first["problems"][0]
    second = ok("strategy_dry_run", {"code": REFERENCE}, who(12))
    assert second["ok"] is True
    made = ok("strategy_create", ARGS, who(12))
    assert made["measure"]["tool_calls"] == 3 and made["measure"]["dry_runs"] == 2 and made["measure"]["llm_calls"] < 20


def test_a_code_that_fails_the_checks_saves_nothing_and_says_the_change(db):
    bad = REFERENCE.replace("def generate_signals(df, params):", "def other(df, params):")
    answer = ok("strategy_create", {**ARGS, "code": bad}, who(13))
    assert answer["created"] is False and answer["problems"][0]["rule"] == "generate_signals" and answer["problems"][0]["fix"]
    for table in ("strategy_bank", "user_rules", "user_rule_versions", "tasks", "journal_entries", "creation_metrics WHERE kind = 'creation'"):
        assert rows(db, f"SELECT 1 FROM {table}") == [], table
    assert not list((hokma_home() / "service" / "strategies").glob("STR-*")) if (hokma_home() / "service" / "strategies").exists() else True


def test_the_same_code_twice_is_not_a_second_strategy_and_a_new_version_goes_on_the_same_card(db):
    made = ok("strategy_create", ARGS, who(14))
    again = ok("strategy_create", ARGS, who(14))
    assert again["created"] is False and again["duplicate"] is True and again["registry_id"] == made["registry_id"]
    changed = REFERENCE.replace('"default": 2.0, "min": 1.0', '"default": 2.5, "min": 1.0')
    nxt = ok("strategy_create", {**ARGS, "code": changed, "bank_id": made["bank_id"], "spec": SPEC + "\nThe ATR multiple is 2.5 by default.\n"}, who(14))
    assert nxt["created"] is True and nxt["code"] == "STR-001" and nxt["version"] == 2 and nxt["registry_id"] == "u_str001_v2"
    assert len(rows(db, "SELECT 1 FROM strategy_bank")) == 1 and (hokma_home() / nxt["folder"] / "versions/v2/strategy.py").is_file()
    assert {"u_str001_v1", "u_str001_v2"} <= set(default_registry().entries)


def test_a_bad_input_is_a_classified_error_and_the_text_is_the_only_input(db):
    assert failed("strategy_create", {**ARGS, "spec": "short"}, who(15))["error_code"] == "bad_input"
    assert failed("strategy_create", {**ARGS, "timeframe": "banana"}, who(15))["error_code"] == "bad_input"
    answer, is_error = call("strategy_dry_run", {"code": REFERENCE, "path": "/etc/passwd"}, who(15))
    assert is_error and "path" in str(answer)                                                         # there is no argument that takes a path
    assert failed("strategy_create", {**ARGS, "summary": "Sells covered calls on the index every week for premium income."}, who(15))["error_code"] == "blocked"
    ok("strategy_dry_run", {"code": REFERENCE, "timeframe": "15m"}, who(15))
