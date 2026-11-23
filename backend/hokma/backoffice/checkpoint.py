"""A long run that can be resumed (SPEC-018: TA-25, TA-26).

Before each node runs, the state that enters it is saved in `run_checkpoints` under the run. An interrupted run is resumed from the node that
did not finish, with the state it had when it entered it. When the run ends the checkpoints are not deleted: they stay as the record of
the run. The signature of the shape of the graph (its nodes and edges) is saved with the run and with every checkpoint: a graph whose
shape changed does not take over the checkpoints of another one, it starts from zero and says so."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from hokma.backoffice import runs
from hokma.db import Actor, Database, get_database
from hokma.desks.core.graph import END, CompiledGraph, apply_message_updates

STEP_WIDTH = 5
NOTICE_NEW_SHAPE = "The shape of the graph changed since the interrupted run: its checkpoints are ignored and the run starts from zero."


class CheckpointError(RuntimeError):
    """The graph or the checkpoint cannot be used as given."""


def signature(nodes: Iterable[str], edges: Iterable[tuple[str, str, str] | tuple[str, str]], *, extra: Iterable[str] = ()) -> str:
    """The signature of a graph: the sha256 of its nodes and edges, sorted, so the order in which it was built does not matter. An edge is
    (source, target) or (source, label, target) for a conditional one. `extra` are other choices that change the run (the analysts picked)."""
    shape = {"nodes": sorted(set(nodes)), "edges": sorted([list(e) if len(e) == 3 else [e[0], "", e[1]] for e in edges]), "extra": sorted(set(extra))}
    return hashlib.sha256(json.dumps(shape, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _parts(graph: CompiledGraph) -> tuple[dict, dict, dict, str, int]:
    try:
        return graph._nodes, graph._edges, graph._cond, graph._entry, graph._limit
    except AttributeError as exc:
        raise CheckpointError("a compiled desk graph is needed") from exc


def shape_of(graph: CompiledGraph) -> tuple[tuple[str, ...], tuple[tuple[str, str, str], ...]]:
    nodes, edges, cond, entry, _limit = _parts(graph)
    out = [("__start__", "", entry)] + [(s, "", t) for s, t in edges.items()]
    out += [(s, label, target) for s, (_router, pmap) in cond.items() for label, target in pmap.items()]
    return tuple(sorted(nodes)), tuple(sorted(out))


def signature_of(graph: CompiledGraph, *, extra: Iterable[str] = ()) -> str:
    nodes, edges = shape_of(graph)
    return signature(nodes, edges, extra=extra)


# ------------------------------------------------------------------------------------------------------- checkpoints

def save(run_id: int, sig: str, step: int, node: str, state: dict[str, Any], path: Sequence[str], *, db: Database | None = None) -> None:
    """The state that enters `node` at `step`. Written once: a step number is never reused inside a run."""
    body = runs.dumps({"state": state, "path": list(path)}, limit=runs.MAX_RECORD_CHARS)
    with (db or get_database()).write() as u:
        u.session.insert_row("run_checkpoints", {"run_id": run_id, "node": f"{step:0{STEP_WIDTH}d}:{node}", "signature": sig, "state_json": body})


@dataclass(frozen=True)
class Point:
    step: int
    node: str
    state: dict[str, Any]
    path: tuple[str, ...]
    signature: str


def last(run_id: int, *, db: Database | None = None) -> Point | None:
    with (db or get_database()).read() as u:
        row = u.session.one("SELECT node, signature, state_json FROM run_checkpoints WHERE run_id = :r ORDER BY node DESC LIMIT 1", {"r": run_id})
    if row is None:
        return None
    step, _, node = row["node"].partition(":")
    body = json.loads(row["state_json"])
    return Point(int(step), node, runs.decode(body["state"]), tuple(body["path"]), row["signature"])


def count(run_id: int, *, db: Database | None = None) -> int:
    with (db or get_database()).read() as u:
        return int(u.session.scalar("SELECT COUNT(*) FROM run_checkpoints WHERE run_id = :r", {"r": run_id}) or 0)


@dataclass(frozen=True)
class Start:
    run_id: int
    resumed: bool
    point: Point | None
    notice: str | None = None


def start(desk_id: int | None, kind: str, sig: str, *, db: Database | None = None) -> Start:
    """Resume the latest run of this desk and kind when it was interrupted and its signature is this one; otherwise open a new run (and
    say so when the latest was interrupted under another shape of the graph)."""
    database = db or get_database()
    with database.read() as u:
        sql, params = "SELECT id, signature, status FROM run_records WHERE kind = :k AND desk_id IS NULL", {"k": kind}
        if desk_id is not None:
            sql, params["d"] = "SELECT id, signature, status FROM run_records WHERE kind = :k AND desk_id = :d", desk_id
        latest = u.session.one(sql + " ORDER BY id DESC LIMIT 1", params)
    notice = None
    if latest is not None and latest["status"] == "interrupted":
        point = last(latest["id"], db=database)
        if point is not None and point.signature == sig and latest["signature"] == sig:
            runs.resume(latest["id"], db=database)
            return Start(latest["id"], True, point)
        notice = NOTICE_NEW_SHAPE
        with database.write() as u:
            u.journal.add("note", "Checkpoint ignored: the shape of the graph changed", actor=Actor.system("backoffice"), outcome="partial",
                          body=f"run {latest['id']} of kind {kind} was interrupted under another signature; a new run starts from zero",
                          refs={"run_id": latest["id"], "kind": kind})
    return Start(runs.begin(desk_id, kind, signature=sig, db=database), False, None, notice)


@dataclass(frozen=True)
class Result:
    run_id: int
    resumed: bool
    state: dict[str, Any]
    path: tuple[str, ...]
    notice: str | None


def run(graph: CompiledGraph, state: dict[str, Any], *, desk_id: int | None, kind: str, extra: Iterable[str] = (),
        db: Database | None = None) -> Result:
    """Run a desk graph node by node with a checkpoint before each one. If a node raises (or the process is stopped), the run is marked
    interrupted and the error goes on; calling `run` again resumes it. When it ends, the record is finished and the checkpoints stay."""
    database = db or get_database()
    sig = signature_of(graph, extra=extra)
    nodes, edges, cond, entry, limit = _parts(graph)
    began = start(desk_id, kind, sig, db=database)
    if began.resumed and began.point is not None:
        current, node, step, path = dict(began.point.state), began.point.node, began.point.step, list(began.point.path)
    else:
        current, node, step, path = dict(state), entry, 0, []
    saved = began.resumed                               # the checkpoint of the node it resumes from is already there
    try:
        while node != END:
            if len(path) >= limit:
                raise CheckpointError(f"recursion limit {limit} reached; path tail: {path[-6:]}")
            if not saved:
                save(began.run_id, sig, step, node, current, path, db=database)
            saved = False
            path.append(node)
            update = nodes[node](current) or {}
            for key, value in update.items():
                current[key] = apply_message_updates(current.get("messages", []), value) if key == "messages" else value
            if node in edges:
                node = edges[node]
            else:
                router, pmap = cond[node]
                label = router(current)
                if label not in pmap:
                    raise CheckpointError(f"router of {node!r} returned {label!r}; declared: {sorted(pmap)}")
                node = pmap[label]
            step += 1
    except BaseException:
        try:
            runs.interrupt(began.run_id, db=database)
        except runs.RunClosed:
            pass
        raise
    runs.finish(began.run_id, "done", outputs={"path": path, "steps": step}, db=database)
    return Result(began.run_id, began.resumed, current, tuple(path), began.notice)
