"""hokma quant list and hokma quant run (SPEC-017: AH-61, TA-91)."""
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd

FIXTURE = Path(__file__).parent / "quant" / "fixtures" / "returns_daily.csv"


def run_cli(*args):
    return subprocess.run([sys.executable, "-m", "hokma_cli.main", *args], capture_output=True, text=True, timeout=180)


def test_list_shows_registry_functions_AH_61():
    out = run_cli("quant", "list", "--module", "risk")
    assert out.returncode == 0 and "risk.var" in out.stdout and "function(s)" in out.stderr
    as_json = json.loads(run_cli("quant", "list", "--json").stdout)
    assert any(f["name"] == "metrics.sharpe" for f in as_json)


def test_run_prints_the_same_number_as_the_module_TA_91():
    from hokma.quant.metrics import ratios

    out = run_cli("quant", "run", "sharpe", "--file", str(FIXTURE))
    assert out.returncode == 0, out.stderr
    printed = json.loads(out.stdout)
    series = pd.read_csv(FIXTURE, index_col=0, parse_dates=True)["return"]
    assert printed["name"] == "metrics.sharpe" and printed["value"] == ratios.sharpe(series).value
    assert "metrics.sharpe" in out.stderr


def test_run_with_param_unknown_and_blocked():
    var = run_cli("quant", "run", "risk.var", "--file", str(FIXTURE), "--param", "level=0.9")
    assert var.returncode == 0 and json.loads(var.stdout)["params"]["level"] == 0.9
    assert run_cli("quant", "run", "nope", "--file", str(FIXTURE)).returncode == 2
    assert run_cli("quant", "run", "metrics.probabilistic_sharpe_ratio", "--file", str(FIXTURE)).returncode == 3
    assert run_cli("quant", "run", "sharpe", "--file", str(FIXTURE), "--param", "bad").returncode == 2


def test_resolve_name_finds_short_names():
    from hokma.quant import registry
    from hokma_cli import quant_cmd

    registry.load_all()
    assert quant_cmd.resolve_name(registry, "sharpe") == "metrics.sharpe"
    assert quant_cmd.resolve_name(registry, "validation.psr") == "validation.psr"
    assert quant_cmd.resolve_name(registry, "no_such_function") == "no_such_function"


def test_quant_command_runs_only_after_the_integrity_gate(tmp_path, monkeypatch, capsys):
    from hokma.kit import integrity
    from hokma_cli import main as cli

    assert "quant" not in cli.NO_INTEGRITY_GATE
    app = tmp_path / "data" / "runtime" / "a" / "app"
    (app / "backend" / "hokma").mkdir(parents=True)
    (app / "backend" / "hokma" / "a.py").write_text("print(1)")
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(integrity, "app_root", lambda: app)
    integrity.write_manifest()
    assert cli.main(["quant", "list", "--module", "risk"]) == 0
    (app / "backend" / "hokma" / "a.py").write_text("print(2)")         # the installed code no longer matches its manifest
    capsys.readouterr()
    assert cli.main(["quant", "list", "--module", "risk"]) == 3
    captured = capsys.readouterr()
    assert "risk.var" not in captured.out and captured.err
