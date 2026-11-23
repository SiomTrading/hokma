"""The quickstart example runs with fixture data and ends without an error (SPEC-017: TA-92)."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_desk_core_quickstart_runs():
    out = subprocess.run([sys.executable, "docs/examples/desk_core_quickstart.py"], cwd=ROOT, capture_output=True, text=True, timeout=240)
    assert out.returncode == 0, out.stderr[-2000:]
    assert '"sharpe"' in out.stdout and '"signal"' in out.stdout
