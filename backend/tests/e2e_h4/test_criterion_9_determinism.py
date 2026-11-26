"""Criterion 9 of SPEC-020: the same spec with the same seed gives the same result byte for byte. Two runs are serialized and hashed, and so are an
optimization, a walk forward and a Monte Carlo made through the tools."""
import hashlib
import json
from dataclasses import replace
from decimal import Decimal as D

import pytest
from backtest import support
from backtest.runner_support import NQ, Fixed, source_of, spec
from backtest.support import requires_engine
from dbsupport import db, env  # noqa: F401  (fixtures)
from e2e_h4.support import SPACE, bars, caller, ok, run_args, same_bytes, use_services

from hokma import backtest
from hokma.backtest import _lib, results
from hokma.backtest.runner import Services

pytestmark = requires_engine
WHO = caller("hokma")


def digest(result) -> str:
    return hashlib.sha256(results.to_json(replace(result, run_id=0)).encode("utf-8")).hexdigest()


def test_two_runs_of_the_same_spec_and_seed_have_the_same_hash_and_the_same_bytes(db, tmp_path):
    scenario = support.bars_of("T1")
    made = []
    for k in (1, 2):
        services = Services(db=db, source=source_of({NQ: scenario}), curve_dir=tmp_path / str(k), strategy=lambda params: Fixed())
        made.append(backtest.run(spec(scenario, seed=7), services=services))
    first, second = made
    assert first.run_id != second.run_id and digest(first) == digest(second) and same_bytes(first, second)
    assert first.spec_hash == second.spec_hash and first.data_hash == second.data_hash
    assert first.realized_pnl_usd == second.realized_pnl_usd == D("100.50")


def test_a_longer_run_through_the_tool_gives_the_same_bytes_twice(db, monkeypatch, tmp_path):
    data = bars(1500)
    use_services(monkeypatch, data, db, tmp_path)
    ids = [ok("backtest_run", run_args(data, seed=9), WHO)["run_id"] for _ in range(2)]
    texts = []
    for run_id in ids:
        saved = ok("backtest_result", {"run_id": run_id, "max_trades": 500}, WHO)
        texts.append(json.dumps({k: v for k, v in saved.items() if k != "run_id"}, sort_keys=True))
    assert texts[0] == texts[1] and hashlib.sha256(texts[0].encode()).hexdigest() == hashlib.sha256(texts[1].encode()).hexdigest()


@pytest.mark.skipif(not _lib.search_available(), reason="the TPE search library has no wheel on this platform")
def test_a_seeded_search_a_walk_forward_and_a_monte_carlo_repeat_exactly(db, monkeypatch, tmp_path):
    data = bars(1500)
    use_services(monkeypatch, data, db, tmp_path)
    searches, walks, carlos = [], [], []
    for _ in range(2):
        found = ok("backtest_optimize", run_args(data, space=SPACE, method="tpe", n_trials=20, objective="net_pnl", presweep=False, seed=5), WHO)
        searches.append(json.dumps({k: v for k, v in found.items() if k not in ("run_id", "n_trials")}, sort_keys=True))
        forward = ok("backtest_walkforward", run_args(data, space=SPACE, scheme="rolling", train=250, test=250, purge=2, embargo=1, seed=5), WHO)
        walks.append(json.dumps({k: v for k, v in forward.items() if k != "run_id"}, sort_keys=True))
        done = ok("backtest_run", run_args(data, seed=5), WHO)
        carlo = ok("backtest_montecarlo", {"run_id": done["run_id"], "n": 200, "method": "placebo", "seed": 5}, WHO)
        carlos.append(json.dumps({k: v for k, v in carlo.items() if k != "of_run_id"}, sort_keys=True))
    assert searches[0] == searches[1] and walks[0] == walks[1] and carlos[0] == carlos[1]
