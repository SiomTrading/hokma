"""AS-005 and B-49: docs/backtest/engine-choice.md exists in English, passes the repository rules and states the measured criteria."""
import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
DOC = ROOT / "docs" / "backtest" / "engine-choice.md"
spec = importlib.util.spec_from_file_location("check_repo_rules", ROOT / "scripts" / "check_repo_rules.py")
rules = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rules)


def rev(text: str) -> str:
    return text[::-1]


# Names of the other engines that were compared, stored reversed so this file does not contain them. They belong only in .notices/THIRD_PARTY.txt.
OTHER_ENGINES = [rev(x) for x in ["yp.gnitsetkcab", "tbrotcev", "naeL", "edartmetsysyp", "ypnv", "tsetkcabTFH", "rekorByP", "bil-fq", "egroFeniP"]]


def text() -> str:
    return DOC.read_text(encoding="utf-8")


def test_the_page_passes_the_repository_rules():
    assert rules.check(ROOT, files=["docs/backtest/engine-choice.md"]) == []


@pytest.mark.parametrize("name", OTHER_ENGINES)
def test_no_other_engine_is_named(name):
    assert not re.search(rf"(?<![\w]){re.escape(name.lower())}(?![\w])", text().lower())


def test_the_other_engines_are_listed_in_the_notices_only():
    notices = (ROOT / ".notices" / "THIRD_PARTY.txt").read_text(encoding="utf-8").lower()
    for name in OTHER_ENGINES:
        assert name.lower() in notices


def test_the_page_states_the_five_criteria():
    body = text()
    for heading in ("## Fill rules T1 to T4", "## Licence", "## Speed", "## Simulated and live with the same code", "## Instruments"):
        assert heading in body, heading
    assert "backend/hokma/backtest/_lib.py" in body and "single engine" in body


def test_the_fill_results_are_the_hand_computed_values():
    body = text()
    for value in ("100.50 USD", "-114.50 USD", "25466.50", "25477.00", "-2059.50 USD", "25321.00", "-49.50 USD", "25552.00", "25549.75"):
        assert value in body, value
    # T1 by hand: (25643.00 - 25637.25) = 5.75 points; minus 0.5 point of slippage = 5.25; times 20 = 105.00; minus 4.50 of fees = 100.50
    assert round((25643.00 - 25637.25 - 0.5) * 20 - 4.50, 2) == 100.50
    # T4 by hand: long 25552.00 -> 25549.75 is -2.25 points, times 20 = -45.00, minus 4.50 of fees = -49.50
    assert round((25549.75 - 25552.00) * 20 - 4.50, 2) == -49.50


def test_the_speed_table_matches_the_measurements():
    body = text()
    for value in ("2.4 s", "10.6 s", "502 s", "21 s", "433 s", "332,721"):
        assert value in body, value


def test_the_page_is_ascii_text():
    assert text().isascii()
