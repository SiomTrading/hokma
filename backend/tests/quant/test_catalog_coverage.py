"""Catalog of what is tested (SPEC-017: AH-65, TA-95): every registered quant function has a test that names its piece id,
and every function is reachable by name through the registry."""
import re
from pathlib import Path

import pytest

from hokma.quant import registry

TESTS = Path(__file__).resolve().parents[1]


def _test_text() -> str:
    parts = []
    for path in list((TESTS / "quant").rglob("*.py")) + [TESTS / "test_quant_data_tools.py", TESTS / "test_quant_cli.py"]:
        if path.name != Path(__file__).name:
            parts.append(path.read_text(encoding="utf-8"))
    return re.sub(r"[-_]", "-", "\n".join(parts))


def test_every_registered_function_has_its_piece_id_in_a_test():
    registry.load_all()
    text = _test_text()
    untested = [(s.name, pid) for s in registry.list(include_blocked=True) for pid in s.piece_ids if pid not in text]
    assert not untested, untested[:20]


def test_every_function_describes_itself_and_is_callable_by_name():
    registry.load_all()
    for spec in registry.list(include_blocked=True):
        info = registry.describe(spec.name).describe()
        assert info["summary"] and info["module"] in {"metrics", "risk", "validation", "options", "fundamentals", "indicators", "stats", "portfolio"}, spec.name
        assert info["piece_ids"], spec.name


def test_blocked_functions_name_their_replacement():
    registry.load_all()
    blocked = [s for s in registry.list(include_blocked=True) if s.blocked]
    assert blocked, "the metrics module blocks the defective library functions"
    for spec in blocked:
        assert spec.replacement and registry.describe(spec.replacement)
