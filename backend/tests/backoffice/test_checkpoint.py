"""A long run that resumes from where it stopped, and the signature of the shape of the graph (SPEC-018: TA-25, TA-26)."""
from decimal import Decimal as D

import pytest
from backoffice_support import db, desks_db, env, ready  # noqa: F401

from hokma.backoffice import checkpoint, runs
from hokma.desks.core.graph import END, START, StateGraph
from hokma.desks.core.state import Message


class Probe:
    """Counts how many times each node ran, remembers the state each one received, and can make one node fail."""

    def __init__(self, fail_at=None):
        self.calls, self.seen, self.fail_at = {}, {}, fail_at

    def node(self, name, update):
        def run(state):
            self.calls[name] = self.calls.get(name, 0) + 1
            self.seen.setdefault(name, dict(state))
            if self.fail_at == name:
                raise RuntimeError(f"{name} broke")
            return update(state)
        return run


def build(probe, *, extra_node=False, target="c"):
    g = StateGraph()
    g.add_node("a", probe.node("a", lambda s: {"trail": ["a"], "price": D("21000.25"), "messages": [Message("user", "NQ", "m0")]}))
    g.add_node("b", probe.node("b", lambda s: {"trail": s["trail"] + ["b"], "legs": ("x", "y")}))
    g.add_node("c", probe.node("c", lambda s: {"trail": s["trail"] + ["c"], "verdict": "buy"}))
    g.add_edge(START, "a")
    g.add_edge("a", "b")
    if extra_node:
        g.add_node("extra", probe.node("extra", lambda s: {"trail": s["trail"] + ["extra"]}))
        g.add_edge("b", "extra")
        g.add_edge("extra", "c")
    else:
        g.add_edge("b", target)
    g.add_edge("c", END)
    return g.compile()


def test_the_signature_changes_with_the_nodes_or_the_edges_and_not_with_the_build_order_TA_26():
    base = checkpoint.signature(["a", "b", "c"], [("a", "b"), ("b", "c")])
    assert base == checkpoint.signature(["c", "b", "a"], [("b", "c"), ("a", "b")])             # the same shape built in another order
    assert base != checkpoint.signature(["a", "b", "c", "d"], [("a", "b"), ("b", "c")])         # a node more
    assert base != checkpoint.signature(["a", "b", "c"], [("a", "b"), ("b", "c"), ("c", "a")])  # an edge more
    assert base != checkpoint.signature(["a", "b", "c"], [("a", "c"), ("b", "c")])              # an edge pointing elsewhere
    assert base != checkpoint.signature(["a", "b", "c"], [("a", "", "b"), ("b", "go", "c")])    # a label on a conditional edge
    assert base != checkpoint.signature(["a", "b", "c"], [("a", "b"), ("b", "c")], extra=["analysts=market"])
    assert len(base) == 64


def test_the_signature_of_a_real_graph_follows_its_shape_TA_26():
    one, same, more = build(Probe()), build(Probe()), build(Probe(), extra_node=True)
    assert checkpoint.signature_of(one) == checkpoint.signature_of(same) != checkpoint.signature_of(more)
    nodes, edges = checkpoint.shape_of(more)
    assert nodes == ("a", "b", "c", "extra") and ("__start__", "", "a") in edges and ("b", "", "extra") in edges


def test_a_conditional_edge_is_part_of_the_signature_TA_26():
    def graph(go):
        g = StateGraph()
        g.add_node("a", lambda s: {})
        g.add_node("b", lambda s: {})
        g.add_node("c", lambda s: {})
        g.add_edge(START, "a")
        g.add_conditional_edges("a", lambda s: "next", {"next": "b", "stop": go})
        g.add_edge("b", "c")
        g.add_edge("c", END)
        return g.compile()

    assert checkpoint.signature_of(graph("c")) != checkpoint.signature_of(graph(END))
    assert checkpoint.signature_of(graph(END)) == checkpoint.signature_of(graph(END))


def test_an_interrupted_run_resumes_from_its_node_with_the_same_state_TA_25(ready):
    first = Probe(fail_at="c")
    with pytest.raises(RuntimeError, match="c broke"):
        checkpoint.run(build(first), {"symbol": "NQ"}, desk_id=ready.desk_id, kind="cycle", db=ready.db)
    row = runs.list_runs(ready.desk_id, kind="cycle", db=ready.db)[0]
    assert row["status"] == "interrupted" and checkpoint.count(row["id"], db=ready.db) == 3
    point = checkpoint.last(row["id"], db=ready.db)
    assert (point.step, point.node, point.path) == (2, "c", ("a", "b"))
    entering_c = first.seen["c"]
    assert point.state["trail"] == ["a", "b"] and point.state["legs"] == ("x", "y") and point.state["price"] == D("21000.25")

    second = Probe()
    out = checkpoint.run(build(second), {"symbol": "NQ"}, desk_id=ready.desk_id, kind="cycle", db=ready.db)
    assert out.resumed and out.run_id == row["id"] and out.notice is None
    assert second.calls == {"c": 1}                                       # a and b were not run again
    assert second.seen["c"] == entering_c                                 # c got exactly the state it had when it stopped
    assert out.path == ("a", "b", "c") and out.state["trail"] == ["a", "b", "c"] and out.state["verdict"] == "buy"
    assert out.state["messages"] == [Message("user", "NQ", "m0")]


def test_when_the_run_ends_the_checkpoints_stay_as_its_record_TA_25(ready):
    out = checkpoint.run(build(Probe()), {"symbol": "NQ"}, desk_id=ready.desk_id, kind="cycle", db=ready.db)
    row = runs.get(out.run_id, db=ready.db)
    assert row["status"] == "done" and row["record"]["outputs"] == {"path": ["a", "b", "c"], "steps": 3}
    assert checkpoint.count(out.run_id, db=ready.db) == 3                  # not deleted
    assert checkpoint.last(out.run_id, db=ready.db).node == "c"
    again = checkpoint.run(build(Probe()), {"symbol": "NQ"}, desk_id=ready.desk_id, kind="cycle", db=ready.db)
    assert not again.resumed and again.run_id != out.run_id                # a finished run is never resumed
    assert checkpoint.count(out.run_id, db=ready.db) == 3 and checkpoint.count(again.run_id, db=ready.db) == 3


def test_a_checkpoint_of_another_shape_is_not_resumed_the_run_starts_from_zero_with_a_notice_TA_26(ready):
    with pytest.raises(RuntimeError):
        checkpoint.run(build(Probe(fail_at="c")), {"symbol": "NQ"}, desk_id=ready.desk_id, kind="cycle", db=ready.db)
    old = runs.list_runs(ready.desk_id, kind="cycle", db=ready.db)[0]
    changed = Probe()
    out = checkpoint.run(build(changed, extra_node=True), {"symbol": "NQ"}, desk_id=ready.desk_id, kind="cycle", db=ready.db)
    assert not out.resumed and out.run_id != old["id"] and out.notice == checkpoint.NOTICE_NEW_SHAPE
    assert changed.calls == {"a": 1, "b": 1, "extra": 1, "c": 1}           # everything ran again, from the start
    assert runs.get(old["id"], db=ready.db)["status"] == "interrupted" and checkpoint.count(old["id"], db=ready.db) == 3     # the old run is left as it was
    with ready.db.read() as u:
        notes = u.journal.list(kind="note", search="Checkpoint ignored")
    assert len(notes) == 1 and notes[0].refs["run_id"] == old["id"]
    quiet = checkpoint.run(build(Probe(), extra_node=True), {"symbol": "NQ"}, desk_id=ready.desk_id, kind="cycle", db=ready.db)
    assert quiet.notice is None                                            # the next run is not told again


def test_a_run_of_another_desk_or_kind_is_not_resumed_TA_25(ready):
    with pytest.raises(RuntimeError):
        checkpoint.run(build(Probe(fail_at="c")), {}, desk_id=ready.desk_id, kind="cycle", db=ready.db)
    other_kind = checkpoint.run(build(Probe()), {}, desk_id=ready.desk_id, kind="weekly", db=ready.db)
    no_desk = checkpoint.run(build(Probe()), {}, desk_id=None, kind="cycle", db=ready.db)
    assert not other_kind.resumed and not no_desk.resumed


def test_the_checkpoint_does_not_keep_a_secret_TA_25(ready):
    probe = Probe(fail_at="c")
    with pytest.raises(RuntimeError):
        checkpoint.run(build(probe), {"api_key": "SENTINEL-KEY-777777", "note": "token=SENTINEL-TOKEN-888888"}, desk_id=ready.desk_id, kind="cycle", db=ready.db)
    with ready.db.read() as u:
        raw = " ".join(r["state_json"] for r in u.session.query("SELECT state_json FROM run_checkpoints"))
    assert "SENTINEL-KEY-777777" not in raw and "SENTINEL-TOKEN-888888" not in raw
