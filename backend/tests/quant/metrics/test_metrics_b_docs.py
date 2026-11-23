"""Metrics part B: the two architecture documents (AS-008, QS-064)."""
import re
import subprocess
import sys
from pathlib import Path

from .metrics_b_qa import EXPECTED

ROOT = Path(__file__).resolve().parents[4]
FORMULAS = ROOT / "docs" / "architecture" / "metric-formulas.md"
UPGRADES = ROOT / "docs" / "architecture" / "metrics-upgrades.md"
FUTURE_WORDS = re.compile(r"\b(will|planned|plans|todo|future|roadmap|someday|later)\b", re.IGNORECASE)


def test_metric_formulas_exists_and_describes_the_code_AS_008():
    text = FORMULAS.read_text(encoding="utf-8")
    assert text.startswith("# Metric formulas") and len(text) > 4000
    assert not FUTURE_WORDS.search(text), FUTURE_WORDS.search(text)
    assert not re.search(r"https?://", text)
    missing = [name for name in EXPECTED if f"`{name}`" not in text]
    assert not missing, missing
    for file_name in ("summary.py", "trades.py", "tail_view.py", "montecarlo.py", "attribution.py", "benchmark.py", "report.py"):
        assert file_name in text


def test_metrics_upgrades_exists_and_is_current_QS_064():
    text = UPGRADES.read_text(encoding="utf-8")
    assert text.startswith("# Metrics upgrades") and len(text) > 2000
    assert not FUTURE_WORDS.search(text), FUTURE_WORDS.search(text)
    assert "Sources read for this document" in text and "period" in text
    assert not re.search(r"https?://", text)
    for name in ("metrics.summary_psr_row", "metrics.risk_of_ruin", "metrics.montecarlo_drawdown", "metrics.report_html"):
        assert name in text


def test_repo_rules_pass_for_the_documents_AS_008_QS_064():
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "check_repo_rules.py")], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
