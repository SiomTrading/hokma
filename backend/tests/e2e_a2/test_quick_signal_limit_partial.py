"""Quick Signal with a limit entry, two contracts, one out at 1R and the stop of the other at breakeven (1.2.4, task 3422): saved as STR-001 and run to done by its code with backtest_run.
The bars are worked out by hand: the limit at 19990 fills in bar 5, one contract leaves at the open of bar 6, the stop of the other goes to 19990 and bar 7 falls through it."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from backtest.runner_support import NQ, source_of
from backtest.support import requires_engine
from code_support import CODER, RESEARCHER, REVIEWER, db, env, make_bank, pg_admin_url, pg_template, pg_url_migrated, put_files, write_spec  # noqa: F401  (fixtures)
from e2e_h4.support import caller, ok, run_args

from hokma.backtest.runner import Services
from hokma.backtest.ticks import Bar
from hokma.core.config import hokma_home
from hokma.factory import spec as factory_spec
from hokma.kit import backtest_tools

STRATEGY = "\"\"\"Code STR-001 version v1: limit entry of two contracts, one out at 1R, the stop of the other at breakeven.\"\"\"\nfrom dataclasses import dataclass\nfrom typing import ClassVar\n\nfrom hokma.factory.orders import OrderIntent, Price, Size, SizeType, buy, reduce_intent\nfrom hokma.factory.params import Param, ParamSchema\n\n\n@dataclass(frozen=True)\nclass QuickLimit:\n    SCHEMA: ClassVar[ParamSchema] = ParamSchema((Param(\"fast\", \"int\", 5, 2, 100), Param(\"slow\", \"int\", 20, 10, 400)))\n    fast: int = 5\n    slow: int = 20\n\n    def init(self, ctx):\n        ctx.set_warmup(2)\n\n    def on_bar(self, ctx):\n        held = ctx.position\n        closes = ctx.series(\"close\")\n        if held is None or held.qty == 0:\n            if len(closes) == 5:\n                return (buy(ctx.contract_id, 2, limit=19990, sl=Price(19980), tp=Price(20010), tag=\"quick_limit\"),)\n            return ()\n        side = \"sell\" if held.qty > 0 else \"buy\"\n        if abs(held.qty) == 2:\n            return (reduce_intent(ctx.contract_id, \"buy\", 1, tag=\"one_r\"),) if ctx.series(\"high\")[-1] >= 20000 else ()\n        return (OrderIntent(ctx.contract_id, side, Size(SizeType.CONTRACTS, 1), \"stop\", None, held.avg_price, reduce_only=True, tag=\"breakeven\"),)\n"
TEST = "import strategy\n\n\ndef test_the_defaults():\n    assert strategy.QuickLimit().fast == 5\n"
START = datetime(2025, 11, 10, 15, 0, tzinfo=timezone.utc)
ROWS = [(20000, 20001, 19999, 20000)] * 5 + [(20000, 20002, 19990, 20001), (20001, 20004, 20000, 20003), (20003, 20004, 19989, 19995)] + [(19995, 19996, 19994, 19995)] * 3


def test_quick_signal_with_limit_entry_partial_exit_and_breakeven_runs_to_done_by_its_code(db, monkeypatch, tmp_path):
    bank_id = make_bank(db, "Quick Signal Limit MNQ", spec=False)
    got = ok("bank_get", {"bank_id": bank_id}, caller("hokma"))
    write_spec(got["folder"], "STR-001", bank_id, "Quick Signal Limit MNQ")
    ok("strategy_brief_write", {"bank_id": bank_id, "slug": "quick", "title": "Quick Signal Limit", "brief": {"summary": "Limit entry, two contracts, one out at 1R, the stop of the other at breakeven."}}, caller(RESEARCHER))
    put_files("quick", STRATEGY, TEST, version=1)
    saved = ok("strategy_code_save", {"bank_id": bank_id, "version": 1}, caller(CODER))
    assert saved["registry_id"] == "u_str001_v1"
    ok("strategy_code_verdict", {"rule_version_id": saved["rule_version_id"], "verdict": "approved"}, caller(REVIEWER))
    series = [Bar(START + timedelta(minutes=i), D(str(o)), D(str(h)), D(str(l)), D(str(c)), D(100)) for i, (o, h, l, c) in enumerate(ROWS)]
    monkeypatch.setattr(backtest_tools, "services", lambda: Services(db=db, registry=factory_spec.default_registry(), source=source_of({NQ: series}), curve_dir=tmp_path))
    base = {k: v for k, v in run_args(series, strategy_id="x", version=1).items() if k not in ("strategy_id", "version")}
    done = ok("backtest_run", {**base, "code": "STR-001"}, caller("hokma"))
    assert done["status"] == "done", done
    text = str(done)
    assert "order_type_unsupported" not in text and "partial_close_unsupported" not in text
    with db.read() as u:
        [trade] = u.session.query("SELECT * FROM backtest_trades WHERE run_id = :i", {"i": done["run_id"]})
    assert trade["qty"] == "2" and D(trade["entry_price"]) == D("19990") and trade["exit_tag"] == "stop"
    assert D("19990") <= D(trade["exit_price"]) < D("20003") and D(trade["pnl_usd"]) > 0          # one contract out above 1R, the other at breakeven: a gain after costs
