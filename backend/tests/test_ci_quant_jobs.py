"""The CI file declares the quant jobs (SPEC-017: OA-71, TA-93, QS-063, SK-120)."""
from pathlib import Path

import yaml

CI = yaml.safe_load((Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))


def test_quant_modules_job_runs_the_module_rules():
    steps = " ".join(str(s.get("run", "")) for s in CI["jobs"]["quant-modules"]["steps"])
    for needle in ("uv lock --check", "test_quant_import_scan.py", "backend/tests/quant", "changelog_merge.py --check", "cyclonedx"):
        assert needle in steps, needle


def test_quant_compat_job_has_a_matrix_of_pandas_and_numpy_pairs():
    matrix = CI["jobs"]["quant-compat"]["strategy"]["matrix"]
    assert len(matrix["pair"]) >= 2 and all({"pandas", "numpy"} <= set(p) for p in matrix["pair"])


def test_the_three_original_jobs_are_still_there():
    assert {"rules", "backend", "dashboard"} <= set(CI["jobs"])
