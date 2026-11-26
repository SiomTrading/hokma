"""The checks of a strategy written as code (SPEC-031 R5 and R7): the static check, the tests it brings, the two fixed tests, the time limit and the report."""
import hashlib
import json
import subprocess
import sys

from code_support import STRATEGY, TEST

from hokma.factory import indicator_library, rule_check, user_api, user_rules
from hokma.factory.usercode import static_check


def folder_with(tmp_path, strategy=STRATEGY, test=TEST):
    folder = tmp_path / "inside_bar"
    folder.mkdir(exist_ok=True)
    (folder / "strategy.py").write_text(strategy, encoding="utf-8", newline="\n")
    (folder / "test_strategy.py").write_text(test, encoding="utf-8", newline="\n")
    return folder


def test_a_good_strategy_passes_the_static_check_its_tests_and_the_fixed_tests(tmp_path):
    report = user_rules.run_rule_check(folder_with(tmp_path))
    assert report["ok"] is True and report["static"] == [] and [t["ok"] for t in report["tests"]] == [True] * 4
    assert report["fixed"]["builds"]["ok"] and report["fixed"]["causal"]["ok"] and report["fixed"]["deterministic"]["ok"] and report["fixed"]["decisions"]["entries"] > 0
    assert report["strategy_sha256"] == hashlib.sha256(STRATEGY.encode()).hexdigest() and report["class"] == "InsideBarBreakout"
    assert report["manifest"]["protective"] is False and report["manifest"]["schema"][0]["name"] == "min_range" and "RESULT green" in report["output"]


def test_the_command_exits_zero_only_when_everything_passes_and_its_last_line_is_the_json(tmp_path):
    good = subprocess.run([sys.executable, "-I", "-m", "hokma.factory.rule_check", str(folder_with(tmp_path))], capture_output=True, text=True, cwd=str(tmp_path))
    assert good.returncode == 0 and json.loads(good.stdout.strip().splitlines()[-1])["ok"] is True
    red_test = TEST.replace('assert decisions[-1].action == "short"', 'assert decisions[-1].action == "long"')
    bad = subprocess.run([sys.executable, "-I", "-m", "hokma.factory.rule_check", str(folder_with(tmp_path, test=red_test))], capture_output=True, text=True, cwd=str(tmp_path))
    assert bad.returncode == 1 and "FAIL test_a_close_below_the_inside_bar_goes_short" in bad.stdout and "RESULT red" in bad.stdout
    assert json.loads(bad.stdout.strip().splitlines()[-1])["ok"] is False


def test_a_red_test_says_the_line_and_the_text_of_the_assert(tmp_path):
    red = TEST.replace('assert decisions[-1].action == "hold"', 'assert decisions[-1].action == "long"')
    report = user_rules.run_rule_check(folder_with(tmp_path, test=red))
    failed = [t for t in report["tests"] if not t["ok"]]
    assert report["ok"] is False and [t["name"] for t in failed] == ["test_a_close_inside_the_inside_bar_does_nothing"]
    assert "test_strategy.py line" in failed[0]["message"] and 'assert decisions[-1].action == "long"' in failed[0]["message"]


def test_code_outside_the_list_of_permitted_names_is_a_static_finding_and_nothing_runs(tmp_path):
    for bad, rule in (("import os\n" + STRATEGY, "import_not_allowed"), (STRATEGY.replace("high[-3]", "high.base[-3]"), "attribute_not_allowed"),
                      (STRATEGY.replace("hold()", "open('x')", 1), "name_not_allowed")):
        report = user_rules.run_rule_check(folder_with(tmp_path, strategy=bad))
        assert report["ok"] is False and report["tests"] == [] and rule in {f["rule"] for f in report["static"]}, rule
    in_test = user_rules.run_rule_check(folder_with(tmp_path, test="import os\n" + TEST))
    assert in_test["ok"] is False and in_test["static"][0]["file"] == "test_strategy.py"


def test_the_long_spelling_of_the_import_is_allowed_and_the_package_cannot_be_named_elsewhere():
    long_form = STRATEGY.replace("from user_api import", "from hokma.factory.user_api import")
    assert static_check.check(long_form, kind="strategy").ok
    assert not static_check.check(STRATEGY.replace("from user_api import", "from hokma.factory.params import"), kind="strategy").ok
    assert not static_check.check("import strategy\n\n\ndef test_x():\n    assert 1\n", kind="strategy").ok          # an assert is only for the test file
    assert static_check.check("import strategy\n\n\ndef test_x():\n    assert strategy\n", kind="test").ok
    assert not static_check.check("import strategy\n\n\ndef test_x(a):\n    assert a\n", kind="test").ok            # a test takes no arguments
    assert not static_check.check("import strategy\n\n\ndef helper():\n    return 1\n", kind="test").ok              # a test file needs a test_ function
    assert not static_check.check("import strategy\n\n\nclass T:\n    pass\n\n\ndef test_x():\n    assert 1\n", kind="test").ok


def test_a_strategy_cannot_size_a_position_by_the_names_of_the_sizers(tmp_path):
    for name in ("sizer_n", "contracts", "risk_pct"):
        bad = STRATEGY.replace("min_range", name)
        report = user_rules.run_rule_check(folder_with(tmp_path, strategy=bad))
        assert report["ok"] is False and any(f["rule"] == "contract" for f in report["static"]), name


def test_the_files_must_exist_and_a_strategy_must_have_tests(tmp_path):
    folder = tmp_path / "empty"
    folder.mkdir()
    assert "strategy.py is missing" in user_rules.run_rule_check(folder)["error"]
    (folder / "strategy.py").write_text(STRATEGY, encoding="utf-8")
    assert "test_strategy.py is missing" in user_rules.run_rule_check(folder)["error"]
    assert user_rules.run_rule_check(folder_with(tmp_path, test="import strategy\n\n\ndef helper():\n    return 1\n"))["ok"] is False


def test_a_strategy_that_never_ends_is_stopped_by_the_time_limit(tmp_path):
    slow = TEST.replace("def test_nothing_happens_before_the_warm_up", "def test_waits():\n    while True:\n        pass\n\n\ndef test_nothing_happens_before_the_warm_up")
    report = user_rules.run_rule_check(folder_with(tmp_path, test=slow), timeout=2)
    assert report["ok"] is False and "more than 2 seconds" in report["error"]


class Peek:
    """A class that looks at the size of the whole array behind the revealed view: the fixed test of the future catches it (the static check also refuses the name)."""

    SCHEMA = user_api.ParamSchema(())

    def init(self, ctx):
        ctx.set_warmup(2)

    def on_bar(self, ctx):
        return user_api.long() if len(ctx.close.base) > 300 and ctx.index % 5 == 0 else user_api.hold()


class Dice:
    calls = 0
    SCHEMA = user_api.ParamSchema(())

    def init(self, ctx):
        pass

    def on_bar(self, ctx):
        Dice.calls += 1
        return user_api.long() if Dice.calls % 7 == 0 else user_api.hold()


def test_the_fixed_test_of_the_future_and_the_one_of_repetition_catch_what_a_hand_written_test_misses():
    assert rule_check.fixed_tests(Peek)["causal"]["ok"] is False and "uses the future" in rule_check.fixed_tests(Peek)["causal"]["message"]
    assert rule_check.fixed_tests(Dice)["deterministic"]["ok"] is False


def test_replay_gives_hold_before_the_warm_up_asks_the_library_for_indicators_and_refuses_what_is_not_a_decision():
    class Cross:
        SCHEMA = user_api.ParamSchema(())

        def init(self, ctx):
            ctx.indicator("fast", "ema", length=2)
            ctx.indicator("slow", "ema", length=4)

        def on_bar(self, ctx):
            return user_api.long() if ctx.series("fast")[-1] > ctx.series("slow")[-1] else user_api.short()

    decisions = user_api.replay(Cross, user_api.bars_from_closes([1, 2, 3, 4, 5, 6, 7, 8]))
    assert [d.action for d in decisions[:4]] == ["hold"] * 4 and {d.action for d in decisions[5:]} == {"long"}

    class Wrong:
        SCHEMA = user_api.ParamSchema(())

        def init(self, ctx):
            pass

        def on_bar(self, ctx):
            return "long"

    try:
        user_api.replay(Wrong, user_api.bars_from_closes([1, 2, 3]))
        raise AssertionError("a string is not a decision")
    except ValueError as exc:
        assert "must return a decision" in str(exc)


def test_a_decision_is_checked_the_units_are_explicit_and_only_an_entry_has_levels():
    assert user_api.long(stop=user_api.Ticks(8), target=user_api.ATR(2.0)).stop == user_api.Ticks(8)
    for bad in (lambda: user_api.long(stop=5), lambda: user_api.Decision("flat", user_api.Price(10)), lambda: user_api.Decision("buy"), lambda: user_api.long(reason="x" * 2000)):
        try:
            bad()
            raise AssertionError("refused")
        except ValueError:
            pass


def test_the_bar_kinds_of_the_light_api_are_the_ones_of_the_library_and_the_indicators_are_all_there():
    assert set(user_api.BAR_KINDS) == set(indicator_library.BAR_KINDS)
    assert set(user_api.INDICATORS) == set(indicator_library.LIBRARY)
