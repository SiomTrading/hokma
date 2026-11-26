"""The condition of the strategy creator (SPEC-031 A2): a strategy created by the flow, approved by the reviewer, kept in the bank, and run by the optimizer from the bank until done.

Everything goes through the entry point of the tools, with the caller each tool belongs to: the researcher writes the requirement, the coder saves the code, the reviewer gives the verdict.
"""
import pytest
from backtest import api_support
from backtest.runner_support import NQ, source_of
from backtest.support import requires_engine
from code_support import RESEARCHER, CODER, REVIEWER, brief, db, env, make_bank, pg_admin_url, pg_template, pg_url_migrated, put_files  # noqa: F401  (fixtures)
from e2e_h4.support import SPACE, bars, call, caller, failed, ok, run_args

from hokma.backtest import trials
from hokma.backtest.runner import Services
from hokma.factory import spec as factory_spec
from hokma.kit import backtest_tools

SLUG = "ema_cross_code"
STRATEGY = """from user_api import ClassVar, Param, ParamSchema, dataclass, flat, hold, long, short


@dataclass(frozen=True)
class EmaCrossCode:
    '''Long while the fast average of the closes is above the slow one, short while it is below, flat when they are equal.'''

    SCHEMA: ClassVar[ParamSchema] = ParamSchema((Param("fast", "int", 5, 2, 100), Param("slow", "int", 20, 3, 400)))
    fast: int = 5
    slow: int = 20

    def __post_init__(self):
        if self.fast >= self.slow:
            raise ValueError("fast must be shorter than slow")

    def init(self, ctx):
        ctx.indicator("fast_ma", "ema", length=self.fast, warmup=self.fast)
        ctx.indicator("slow_ma", "ema", length=self.slow, warmup=self.slow)

    def on_bar(self, ctx):
        fast, slow = ctx.series("fast_ma")[-1], ctx.series("slow_ma")[-1]
        if not (fast == fast and slow == slow):
            return hold()
        if fast > slow:
            return long()
        if fast < slow:
            return short()
        return flat()
"""
TEST = """import strategy
from user_api import bars_from_closes, replay


def test_a_rising_market_is_long():
    decisions = replay(strategy.EmaCrossCode, bars_from_closes([float(x) for x in range(100, 160)]))
    assert decisions[-1].action == "long"


def test_a_falling_market_is_short():
    decisions = replay(strategy.EmaCrossCode, bars_from_closes([float(x) for x in range(160, 100, -1)]))
    assert decisions[-1].action == "short"


def test_a_flat_market_does_nothing_new_until_it_has_a_view():
    decisions = replay(strategy.EmaCrossCode, bars_from_closes([100.0] * 60))
    assert decisions[-1].action == "flat"


def test_it_waits_for_the_slow_average():
    decisions = replay(strategy.EmaCrossCode, bars_from_closes([float(x) for x in range(100, 160)]))
    assert [d.action for d in decisions[:19]] == ["hold"] * 19
"""
CASES = [{"name": "rising", "closes": [float(x) for x in range(100, 130)], "expect": "long"}, {"name": "falling", "closes": [float(x) for x in range(130, 100, -1)], "expect": "short"},
         {"name": "flat", "closes": [100.0] * 30, "expect": "flat"}]


@pytest.fixture
def to_bank(db, env):
    """The road of the creator up to the approval: the strategy of the bank, the requirement, the two files, the save and the verdict."""
    bank_id = make_bank(db, "EMA cross as code")
    data = brief(summary="Goes long while the fast exponential average is above the slow one and short while it is below.", inputs=["close", "ema"],
                 params=[{"name": "fast", "type": "int", "value": 5, "min": 2, "max": 100, "origin": "proposed"}, {"name": "slow", "type": "int", "value": 20, "min": 3, "max": 400, "origin": "proposed"}],
                 cases=CASES)
    written = ok("strategy_brief_write", {"bank_id": bank_id, "slug": SLUG, "title": "EMA cross as code", "brief": data}, caller(RESEARCHER))
    put_files(SLUG, STRATEGY, TEST)
    saved = ok("strategy_code_save", {"rule_id": written["rule_id"]}, caller(CODER))
    assert saved["saved"] is True and saved["tests"] == 4
    return {"bank_id": bank_id, "rule": written, "saved": saved}


def verdict(world):
    return ok("strategy_code_verdict", {"rule_version_id": world["saved"]["rule_version_id"], "verdict": "approved", "findings": []}, caller(REVIEWER))


def point_the_tools_at_the_strategy(monkeypatch, db, series, folder):
    monkeypatch.setattr(backtest_tools, "services", lambda: Services(db=db, registry=factory_spec.default_registry(), source=source_of({NQ: series}), curve_dir=folder))


@requires_engine
def test_a_strategy_created_by_the_flow_is_approved_kept_in_the_bank_and_the_optimizer_runs_it_from_the_bank_until_done(to_bank, db, monkeypatch, tmp_path):
    series = bars(1500)
    point_the_tools_at_the_strategy(monkeypatch, db, series, tmp_path)
    registry_id = to_bank["saved"]["registry_id"]
    # before the reviewer's verdict nothing of it exists for the optimizer
    not_yet, is_error = call("backtest_optimize", run_args(series, strategy_id=registry_id, space=SPACE, method="grid", n_trials=50, objective="net_pnl"), caller("hokma"))
    assert is_error or (not_yet["best"] is None and not_yet["failed_trials"] > 0)                  # every point failed: the strategy is not there
    assert registry_id not in factory_spec.default_registry().entries
    before = trials.count(registry_id)
    approved = verdict(to_bank)
    assert (approved["status"], approved["registry_id"], approved["bank_version"]) == ("generated", registry_id, 2)
    # the bank has it, with its version and its origin
    got = ok("bank_get", {"bank_id": to_bank["bank_id"]}, caller("hokma"))
    assert got["version"]["strategy_id"] == registry_id and got["version"]["version"] == 2 and got["version"]["origin"] == {"kind": "hokma"}
    assert got["version"]["agent_reviewed"] is True and got["version"]["owner_reviewed"] is False and got["version"]["code"] is None
    listed = ok("bank_list", {}, caller("hokma"))["rows"]
    assert [(r["strategy_id"], r["origin"]["kind"], r["needs_new_rule"]) for r in listed] == [(registry_id, "hokma", False)]
    # the researcher (his own door, with the tests of the spec) and any caller run it from the bank
    version = approved["spec_version"]
    ran = ok("backtest_run", run_args(series, strategy_id=registry_id, version=version, params={"fast": 5, "slow": 20}), caller(RESEARCHER))
    assert ran["status"] == "done" and ran["trades"] >= 3
    grid = ok("backtest_optimize", run_args(series, strategy_id=registry_id, version=version, space=SPACE, method="grid", n_trials=50, objective="net_pnl"), caller("hokma"))
    assert grid["status"] == "done" and grid["best"] and grid["n_trials"] == trials.count(registry_id) > before
    # the same logic written as a piece of the factory gives the same run
    native = ok("backtest_run", run_args(series, strategy_id="ema_cross", version=1, params={"fast": 5, "slow": 20}), caller("hokma"))
    assert (native["trades"], native["realized_pnl_usd"]) == (ran["trades"], ran["realized_pnl_usd"])
    # nothing about real money was opened by any of this
    assert failed("factory_propose", {"strategy_id": registry_id, "version": version}, caller(RESEARCHER))["error_code"] in ("bad_input", "blocked")



@requires_engine
def test_ian_launches_a_backtest_and_an_optimization_on_a_strategy_of_the_bank_by_bank_id_and_the_verdict_stays_readable(to_bank, db, monkeypatch, tmp_path):
    from e2e_h4.ian import offered_tools

    series = bars(1500)
    point_the_tools_at_the_strategy(monkeypatch, db, series, tmp_path)
    ian = caller(RESEARCHER)
    assert {"backtest_run", "backtest_optimize", "optimizer_verdict", "optimizer_verdict_get", "audit_thresholds_get", "library_list", "library_request"} <= set(offered_tools())
    assert not {"audit_thresholds_set", "audit_thresholds_edit", "optimizer_verdict_edit"} & set(offered_tools())
    base = {k: v for k, v in run_args(series, strategy_id="x", version=1).items() if k not in ("strategy_id", "version")}
    # a strategy with no approved code has no saved spec: nothing to run, and the answer says so
    assert failed("backtest_run", {**base, "bank_id": to_bank["bank_id"]}, ian)["error_code"] == "no_data"
    assert failed("backtest_run", base, ian)["error_code"] == "bad_input"                              # neither a bank id nor a strategy id
    assert failed("backtest_run", {**base, "bank_id": 999}, ian)["error_code"] == "bad_input"
    approved = verdict(to_bank)
    registry_id, version = approved["registry_id"], approved["spec_version"]
    assert failed("backtest_run", {**base, "bank_id": to_bank["bank_id"], "strategy_id": "ema_cross"}, ian)["error_code"] == "bad_input"      # an id that is not the bank's
    ran = ok("backtest_run", {**base, "bank_id": to_bank["bank_id"], "params": {"fast": 5, "slow": 20}}, ian)
    by_id = ok("backtest_run", {**base, "strategy_id": registry_id, "version": version, "params": {"fast": 5, "slow": 20}}, ian)
    assert ran["status"] == "done" and (ran["trades"], ran["realized_pnl_usd"]) == (by_id["trades"], by_id["realized_pnl_usd"])
    before = trials.count(registry_id)
    grid = ok("backtest_optimize", {**base, "bank_id": to_bank["bank_id"], "space": SPACE, "method": "grid", "n_trials": 50, "objective": "net_pnl"}, ian)
    assert grid["status"] == "done" and grid["best"] and trials.count(registry_id) > before
    # the verdict is made from his own tool and is there to be read again
    assert failed("optimizer_verdict_get", {"strategy_id": registry_id, "version": version}, ian)["error_code"] == "no_data"
    made = ok("optimizer_verdict", {"strategy_id": registry_id, "version": version}, ian)
    assert made["advisory"] is True
    read = ok("optimizer_verdict_get", {"strategy_id": registry_id, "version": version}, ian)
    assert read == made and len(read["checks"]) == 24
    assert ok("audit_thresholds_get", {"line": "DT"}, ian)["measures"]
    assert any(i["name"] == "ema" for i in ok("library_list", {"kind": "indicator"}, ian)["items"])
