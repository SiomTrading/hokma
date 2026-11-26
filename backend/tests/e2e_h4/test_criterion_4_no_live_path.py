"""Criterion 4 of SPEC-020: the service has no road to a broker and no tool of this spec sends an order. The scan of the imports covers the packages of the
service and the two tool files; each tool is then called with every connector and every door to a broker replaced by a trap that fails the test if it is touched."""
import ast
import json
from pathlib import Path

from backtest.support import requires_engine
from backtest.test_no_live_path import FORBIDDEN_PACKAGES, SCANNED, files, violations
from dbsupport import db, env  # noqa: F401  (fixtures)
from e2e_h4.support import SPACE, bars, caller, ok, run_args, spec_text, use_services

from hokma.kit import backtest_tools, factory_tools, hokma_tools

KIT = Path(__file__).resolve().parents[3] / "backend" / "hokma" / "kit"
TOOL_FILES = (KIT / "backtest_tools.py", KIT / "factory_tools.py")
TOOLS = {**backtest_tools.TOOLS, **factory_tools.TOOLS}
DOORS = ("hokma.exec.submit.submit", "hokma.exec.submit.flatten", "hokma.exec.submit.dispatch", "hokma.exec.approvals.approve", "hokma.exec.approvals.send_approved",
         "hokma.broker.service.place", "hokma.broker.service.replace", "hokma.broker.service.cancel", "hokma.broker.service.preview",
         "hokma.broker.sim.SimBroker.place_order", "hokma.broker.sim.SimBroker.replace_order", "hokma.broker.schwab.SchwabBroker.place_order",
         "hokma.broker.schwab.SchwabBroker.replace_order", "hokma.broker.schwab.SchwabBroker.cancel_order")


def test_the_scan_of_the_service_is_clean_and_covers_both_packages_and_both_tool_files():
    assert {p.name for p in SCANNED} == {"backtest", "factory"} and files()
    for path in (*files(), *TOOL_FILES):
        package = "hokma.kit" if path in TOOL_FILES else "hokma.backtest"
        assert violations(path.read_text(encoding="utf-8"), package, dynamic_ok=path.name in ("registry.py", "_h3.py")) == [], path


def test_no_tool_file_imports_any_module_that_places_changes_or_approves_an_order():
    for path in TOOL_FILES:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            for name in names:
                parts = name.split(".")
                assert not (parts[:2] == ["hokma", "exec"]) and parts[:3] not in (["hokma", "broker", "service"], ["hokma", "broker", "sim"], ["hokma", "broker", "schwab"]), (path.name, name)
                assert not (len(parts) > 1 and parts[0] == "hokma" and parts[1] in FORBIDDEN_PACKAGES), (path.name, name)


PREMIUM = {"premium_calibrate", "premium_switch_state"}        # they name a desk to measure or read its switch for selling premium: they send nothing


def test_every_tool_of_the_spec_is_listed_with_no_order_in_its_name_or_its_arguments():
    for name, (description, props, required, handler, writes) in TOOLS.items():
        assert name in hokma_tools.TOOLS and "order" not in name and "exec" not in name and "broker" not in name, name
        assert not set(props) & ({"order", "orders", "account_id", "broker", "proposal", "legs", "side", "qty"} | (set() if name in PREMIUM else {"desk_id"})), name
    assert set(TOOLS) == set(backtest_tools.TOOLS) | set(factory_tools.TOOLS) and len(TOOLS) == 14


class Trap(Exception):
    pass


def spring(monkeypatch):
    """Replace every door to a broker by a function that fails: a tool that reaches one fails its test."""
    def trap(*args, **kwargs):
        raise Trap("a tool of the backtest reached a door to a broker")

    for dotted in DOORS:
        monkeypatch.setattr(dotted, trap)


@requires_engine
def test_the_backtest_tools_run_with_every_door_to_a_broker_trapped(db, monkeypatch, tmp_path):
    spring(monkeypatch)
    data = bars(1500)
    use_services(monkeypatch, data, db, tmp_path)
    who = caller("hokma")
    done = ok("backtest_run", run_args(data), who)
    ok("backtest_status", {"run_id": done["run_id"]}, who)
    ok("backtest_result", {"run_id": done["run_id"]}, who)
    ok("backtest_optimize", run_args(data, space=SPACE, method="grid", n_trials=50, objective="net_pnl"), who)
    ok("backtest_walkforward", run_args(data, space=SPACE, scheme="rolling", train=250, test=250, purge=2, embargo=1), who)
    ok("backtest_montecarlo", {"run_id": done["run_id"], "n": 100, "method": "placebo", "seed": 1}, who)
    ok("backtest_evidence", {"strategy_id": "cross", "version": 1}, who)


def test_the_factory_tools_run_with_every_door_to_a_broker_trapped(db, monkeypatch):
    spring(monkeypatch)
    who = caller("strategy-researcher")
    ok("factory_list", {}, who)
    ok("factory_describe", {"id": "ema_cross"}, who)
    ok("factory_draft", {"spec_yaml": spec_text("no_orders")}, who)
    assert ok("factory_propose", {"strategy_id": "no_orders", "version": 1}, who)["status"] == "proposed"
    with db.read() as u:
        assert u.session.scalar("SELECT COUNT(*) FROM exec_proposals") == 0 and u.session.scalar("SELECT COUNT(*) FROM broker_orders") == 0


def test_a_tool_that_is_given_an_order_in_its_arguments_refuses_it_before_it_runs(db):
    for name in ("backtest_run", "factory_propose", "factory_draft"):
        text, is_error = hokma_tools.call(name, {"strategy_id": "x", "version": 1, "spec_yaml": "x", "order": {"legs": []}}, caller("hokma"))
        assert is_error and "unknown argument: order" in text
    assert not any(json.dumps(hokma_tools.TOOLS[n][1]).count("order") for n in TOOLS)
