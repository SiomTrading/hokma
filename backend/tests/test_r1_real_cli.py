"""R1 against the real `claude` CLI (test P12): a hook that exits with 2 blocks even with bypassPermissions, wins
over another hook's "allow", and cannot be switched off by a settings file in the working folder.

Skipped by itself when the CLI is not installed or not signed in, or when HOKMA_R1_SKIP=1. It makes a few short calls
with the smallest model. The same check is `python scripts/r1_probe.py`. Windows and macOS: run it there."""
import importlib.util
import os
from pathlib import Path

import pytest

PROBE = Path(__file__).resolve().parents[2] / "scripts" / "r1_probe.py"


def load_probe():
    spec = importlib.util.spec_from_file_location("r1_probe", PROBE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_r1_exit_2_blocks_in_every_situation_that_matters():
    if os.environ.get("HOKMA_R1_SKIP") == "1":
        pytest.skip("HOKMA_R1_SKIP=1")
    probe = load_probe()
    _exe, reason = probe.claude_state()
    if reason:
        pytest.skip(reason)
    result = probe.run_probe()
    rows = result["rows"]
    print(f"claude {result['cli_version']} on {result['system']}")
    for row in rows:
        print(f"  {row['result']:<13} {row['name']}: {row['detail']}")
    assert not [r for r in rows if r["result"] == "FAIL"], rows          # a file was deleted: the design is broken
    inconclusive = [r for r in rows if r["result"] != "pass"]
    if inconclusive:
        pytest.skip(f"the model did not try the delete in {len(inconclusive)} of {len(rows)} runs; run it again")
