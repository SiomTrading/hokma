"""The flow of the strategy creator from the card to the optimizer (1.2.3): code STR-{NNN} and folder with the card, the spec, version v1 written in versions/v1/, saved and registered as u_str001_v1,
the verdict of another agent, everything readable from bank_get without the database, the optimizer by code, the summary in results/, and the same strategy found again after a restart of the registry."""
import json

from backtest.runner_support import NQ, source_of
from backtest.support import requires_engine
from code_support import CODER, RESEARCHER, REVIEWER, brief, db, env, make_bank, pg_admin_url, pg_template, pg_url_migrated, put_files, write_spec  # noqa: F401  (fixtures)
from e2e_h4.support import bars, call, caller, failed, ok, run_args

from hokma.backtest.runner import Services
from hokma.core.config import hokma_home
from hokma.factory import spec as factory_spec
from hokma.factory import user_rules
from hokma.kit import backtest_tools

STRATEGY = "\"\"\"Code STR-001, version v1, spec service/strategies/STR-001-quick-signal-mnq-2m/spec.md.\n\nQuick Signal on MNQ 2m: the trend is the EMA 20 over the EMA 120, the signal is an engulfing candle in that direction, 2 contracts. The default entry is a market order with the stop at the low of the\nsignal candle and the target at 2R, which the backtest of the engine can fill. With full = 1 the entry is a limit at the high of the candle, one contract goes out at 1R, the stop of the other\ngoes to breakeven and it goes out at 2R: the engine does not fill limit orders or partial exits yet, so that variant only runs live.\n\"\"\"\nfrom dataclasses import dataclass\nfrom typing import ClassVar\n\nfrom hokma.factory.indicator_library import ema\nfrom hokma.factory.orders import OrderIntent, Price, Size, SizeType, buy, sell\nfrom hokma.factory.params import Param, ParamSchema\n\n\n@dataclass(frozen=True)\nclass QuickSignal:\n    SCHEMA: ClassVar[ParamSchema] = ParamSchema((Param(\"slow\", \"int\", 120, 10, 400), Param(\"fast\", \"int\", 20, 2, 100), Param(\"contracts\", \"int\", 2, 2, 10), Param(\"full\", \"int\", 0, 0, 1)))\n    slow: int = 120\n    fast: int = 20\n    contracts: int = 2\n    full: int = 0\n\n    def init(self, ctx):\n        ctx.indicator(\"ema_slow\", ema, source=\"close\", warmup=self.slow, length=self.slow)\n        ctx.indicator(\"ema_fast\", ema, source=\"close\", warmup=self.fast, length=self.fast)\n        ctx.set_warmup(self.slow + 2)\n\n    def on_bar(self, ctx):\n        held = ctx.position\n        if held is not None and held.qty != 0:\n            if self.full == 1 and abs(held.qty) == 1:\n                side = \"sell\" if held.qty > 0 else \"buy\"\n                return (OrderIntent(ctx.contract_id, side, Size(SizeType.CONTRACTS, 1), \"stop\", None, held.avg_price, reduce_only=True, tag=\"breakeven\"),)\n            return ()\n        o, h, l, c = ctx.series(\"open\"), ctx.series(\"high\"), ctx.series(\"low\"), ctx.series(\"close\")\n        fast, slow = ctx.series(\"ema_fast\")[-1], ctx.series(\"ema_slow\")[-1]\n        if len(c) < 2 or fast != fast or slow != slow:\n            return ()\n        bull = c[-1] > o[-1] and c[-2] < o[-2] and c[-1] >= o[-2] and o[-1] <= c[-2]\n        bear = c[-1] < o[-1] and c[-2] > o[-2] and c[-1] <= o[-2] and o[-1] >= c[-2]\n        if fast > slow and bull:\n            return self.orders(ctx, \"buy\", float(h[-1]), float(l[-1]), float(c[-1]))\n        if fast < slow and bear:\n            return self.orders(ctx, \"sell\", float(l[-1]), float(h[-1]), float(c[-1]))\n        return ()\n\n    def orders(self, ctx, side, entry, stop, close):\n        make = buy if side == \"buy\" else sell\n        sign = 1.0 if side == \"buy\" else -1.0\n        if self.full == 1:\n            risk = abs(entry - stop)\n            if risk <= 0:\n                return ()\n            first = make(ctx.contract_id, 1, limit=entry, sl=Price(stop), tp=Price(entry + sign * risk), tag=\"one_r\")\n            second = make(ctx.contract_id, self.contracts - 1, limit=entry, sl=Price(stop), tp=Price(entry + sign * 2.0 * risk), tag=\"two_r\")\n            return (first, second)\n        risk = abs(close - stop)\n        if risk <= 0:\n            return ()\n        return (make(ctx.contract_id, self.contracts, sl=Price(stop), tp=Price(close + sign * 2.0 * risk), tag=\"quick_signal\"),)\n"
TEST = "import strategy\nfrom hokma.factory.strategy import replay_intents\n\n\ndef run(rows):\n    bars = {\"open\": [r[0] for r in rows], \"high\": [r[1] for r in rows], \"low\": [r[2] for r in rows], \"close\": [r[3] for r in rows], \"volume\": [100.0] * len(rows)}\n    return replay_intents(strategy.QuickSignal(), bars, times=list(range(len(rows))), contract_id=\"MNQ\").intents\n\n\ndef rising():\n    rows = []\n    last = 1000.0\n    for i in range(198):\n        close = 1000.0 + 0.5 * i\n        rows.append((last, close + 0.1, last - 0.1, close))\n        last = close\n    return rows\n\n\ndef test_the_defaults_are_two_contracts_with_the_market_entry():\n    one = strategy.QuickSignal()\n    assert (one.slow, one.fast, one.contracts, one.full) == (120, 20, 2, 0)\n\n\ndef test_a_market_that_never_engulfs_orders_nothing():\n    assert run(rising()) == ()\n\n\ndef test_a_bull_engulfing_in_an_uptrend_orders_two_contracts_with_stop_and_target():\n    rows = rising() + [(1100.0, 1100.1, 1098.9, 1099.0), (1098.8, 1101.0, 1098.7, 1100.5)]\n    done = run(rows)\n    assert len(done) == 1 and done[0][0] == 200\n    [intent] = done[0][1]\n    assert intent.side == \"buy\" and intent.size.value == 2 and intent.order_type == \"market\" and intent.reduce_only is False\n    assert intent.sl.value < 1100.5 < intent.tp.value\n\n\ndef test_a_bear_engulfing_in_an_uptrend_does_nothing():\n    rows = rising() + [(1099.0, 1101.0, 1098.9, 1100.9), (1101.1, 1101.2, 1098.0, 1098.5)]\n    assert run(rows) == ()\n"
SPACE = {"axes": [{"name": "fast", "kind": "int", "low": 10, "high": 20, "step": 10}], "constraints": []}


@requires_engine
def test_quick_signal_goes_from_the_card_to_the_optimizer_and_is_found_again_by_its_code_after_a_restart(db, monkeypatch, tmp_path):
    bank_id = make_bank(db, "Quick Signal MNQ 2m", spec=False)
    got = ok("bank_get", {"bank_id": bank_id}, caller("hokma"))
    assert got["code"] == "STR-001" and got["folder"] == "service/strategies/STR-001-quick-signal-mnq-2m" and got["creator"] == []
    assert (hokma_home() / got["folder"]).is_dir()
    # no spec, no brief and no code
    assert failed("strategy_brief_write", {"bank_id": bank_id, "slug": "quick", "title": "Quick Signal", "brief": brief()}, caller(RESEARCHER))["error_code"] == "blocked"
    # the spec first, with its header
    write_spec(got["folder"], "STR-001", bank_id, "Quick Signal MNQ 2m")
    written = ok("strategy_brief_write", {"bank_id": bank_id, "slug": "quick", "title": "Quick Signal", "brief": {"summary": "EMA 20 over EMA 120, engulfing candle, two contracts, stop at the low and target at 2R."}}, caller(RESEARCHER))
    assert written["folder"] == got["folder"] and written["spec_path"] == got["folder"] + "/spec.md"
    seen = ok("strategy_code_get", {"bank_id": bank_id}, caller(CODER))             # before any code the spec and the files still answer
    assert seen["stage"] == "no_code" and seen["spec"].startswith("# Quick Signal") and seen["files"] == ["spec.md"] and seen["draft_code"] is None
    # version v1 in versions/v1/
    folder = put_files("quick", STRATEGY, TEST, version=1)
    assert folder == hokma_home() / got["folder"] / "versions" / "v1"
    dry = ok("strategy_code_save", {"bank_id": bank_id, "version": 1, "dry_run": True}, caller(CODER))
    assert dry["ok"] is True, dry
    saved = ok("strategy_code_save", {"bank_id": bank_id, "version": 1}, caller(CODER))
    assert saved["saved"] and saved["registry_id"] == "u_str001_v1" and saved["str_code"] == "STR-001" and saved["file"] == got["folder"] + "/versions/v1/strategy.py"
    # the author does not give the verdict; another agent does
    assert failed("strategy_code_verdict", {"rule_version_id": saved["rule_version_id"], "verdict": "approved"}, caller(CODER))["error_code"] == "blocked"
    approved = ok("strategy_code_verdict", {"rule_version_id": saved["rule_version_id"], "verdict": "approved"}, caller(REVIEWER))
    assert approved["status"] == "generated" and approved["registry_id"] == "u_str001_v1"
    spec = (hokma_home() / got["folder"] / "spec.md").read_text(encoding="utf-8")
    assert "## Versions" in spec and "approved, ready for backtest" in spec and saved["code_sha256"] in spec
    # everything is readable without the database
    shown = ok("bank_get", {"bank_id": bank_id}, caller("hokma"))
    [view] = shown["creator"]
    assert view["str_code"] == "STR-001" and view["spec_path"] == got["folder"] + "/spec.md" and view["current"]["registry_id"] == "u_str001_v1"
    assert view["current"]["code_sha256"] == saved["code_sha256"] and view["saved_files"] == [{"version": 1, "file": got["folder"] + "/versions/v1/strategy.py"}]
    assert ok("bank_list", {}, caller("hokma"))["rows"][0]["code"] == "STR-001"
    # the optimizer by code
    series = bars(1500)
    monkeypatch.setattr(backtest_tools, "services", lambda: Services(db=db, registry=factory_spec.default_registry(), source=source_of({NQ: series}), curve_dir=tmp_path))
    base = {k: v for k, v in run_args(series, strategy_id="x", version=1).items() if k not in ("strategy_id", "version")}
    done = ok("backtest_optimize", {**base, "code": "STR-001", "space": SPACE, "method": "grid", "n_trials": 10, "objective": "net_pnl"}, caller("hokma"))
    assert done["status"] == "done", done
    [summary] = sorted((hokma_home() / got["folder"] / "results").glob("OPT-1-*.md"))
    text = summary.read_text(encoding="utf-8")
    assert "Code: STR-001" in text and "u_str001_v1" in text and "Status: done" in text
    # the variant with two limit entries of different targets is refused with its own code: the engine holds one target per position
    refused = call("backtest_run", {**base, "code": "STR-001", "params": {"full": 1}}, caller("hokma"))
    assert "multiple_targets_unsupported" in json.dumps(refused[0]) or refused[1]
    # another day: the registry is built again from the bank and finds the strategy by its code
    user_rules._LOADED.clear()
    registry = factory_spec.default_registry()
    assert "u_str001_v1" in registry.entries and user_rules.LOAD_PROBLEMS == {}
    again = ok("backtest_run", {**base, "code": "STR-001"}, caller("hokma"))
    assert again["status"] == "done"
    # a file that changed is not loaded and the registry says why
    path = folder / "strategy.py"
    path.write_text(path.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
    user_rules._LOADED.clear()
    assert "u_str001_v1" not in factory_spec.default_registry().entries
    assert "sha256 changed" in user_rules.LOAD_PROBLEMS["u_str001_v1"]
