"""The record of every cycle of a desk (SPEC-018: AH-24, AH-64, TA-84).

- `record`: one row of `run_records` per cycle with its inputs, outputs, decisions and order ids. A record is written once. A long
  run that is checkpointed (`checkpoint.py`) is `begin` (running), may be `interrupt`ed and `resume`d, and is `finish`ed exactly once into
  `done` or `failed`. There is no function that edits or deletes a record: a finished one refuses every change (`RunClosed`).
- `write_receipt`: the JSON receipt of a run in the folder of the desk. Two runs never share a file name.
- `state_log`: one JSON file per date with the whole trace of the cycle, readable back into the same state.

Nothing written here carries a key, a token or an account identifier: every value goes through `clean` first."""
from __future__ import annotations

import json
import os
import re
import secrets as _random
import tempfile
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import fields, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from hokma.backoffice import timing
from hokma.db import Database, get_database
from hokma.db.timeutil import to_db_time, utcnow
from hokma.desks.core import paths
from hokma.desks.core.safety import MASK, safe_ticker_component, scrub_text
from hokma.desks.core.state import Message, RemoveMessage, ToolCall

FINAL_STATUSES = ("done", "failed")
KIND_RE = re.compile(r"^[a-z][a-z0-9_\-]{0,39}$")
MAX_RECORD_CHARS = 2_000_000
MAX_LIST = 1000
BEGIN_LOCK = 3337_0001                  # the Postgres advisory lock that makes the limits of `begin` atomic between processes
RECEIPTS_DIR = "receipts"
STATE_DIR = "state"
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_LOCK = threading.Lock()
_DATACLASSES = {"Message": Message, "ToolCall": ToolCall, "RemoveMessage": RemoveMessage}
# `scrub_text` takes time that grows with the square of an unbroken run of letters (its URL pattern starts at every letter). A run this long is a
# blob, not text: it is replaced by a marker before scrubbing, so a record can never be made slow by a string with no spaces.
MAX_UNBROKEN = 400
_BLOB = re.compile(r"\S{%d,}" % (MAX_UNBROKEN + 1))

# A key that names a secret loses its value whatever the value looks like (the value may be a plain number).
_SECRET_KEY = re.compile(r"(^|[_\-\s])(api[_\-]?key|key|token|secret|password|passwd|authorization|cookie|account[_\-]?(number|num|hash|id)|"
                         r"hash[_\-]?value)($|[_\-\s])", re.IGNORECASE)


class RunError(ValueError):
    """A record or a file was refused because of its shape or size."""


class RunClosed(RuntimeError):
    """The record is finished: it is never edited again."""


# ------------------------------------------------------------------------------------------------------------ the codec

def encode(value: Any) -> Any:
    """A value as plain JSON data that `decode` brings back exactly: Decimal, tuple and the message classes of the desk state are tagged.
    A type it does not know is an error, never a string: a state that cannot be read back is not saved."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise RunError("a number in a record must be finite")
        return value
    if isinstance(value, Decimal):
        return {"__dec__": str(value)}
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise RunError("keys of a record are text")
            out[k] = encode(v)
        return out
    if isinstance(value, tuple):
        return {"__tuple__": [encode(v) for v in value]}
    if isinstance(value, (list, set, frozenset)):
        items = sorted(value, key=repr) if isinstance(value, (set, frozenset)) else value
        return [encode(v) for v in items]
    if is_dataclass(value) and not isinstance(value, type) and type(value).__name__ in _DATACLASSES:
        return {"__dc__": type(value).__name__, **{f.name: encode(getattr(value, f.name)) for f in fields(value)}}
    raise RunError(f"a value of type {type(value).__name__} cannot be saved in a record")


def decode(value: Any) -> Any:
    if isinstance(value, list):
        return [decode(v) for v in value]
    if isinstance(value, dict):
        if set(value) == {"__dec__"}:
            return Decimal(value["__dec__"])
        if set(value) == {"__tuple__"}:
            return tuple(decode(v) for v in value["__tuple__"])
        if "__dc__" in value:
            cls = _DATACLASSES.get(value["__dc__"])
            if cls is None:
                raise RunError("unknown class in a saved record")
            return cls(**{k: decode(v) for k, v in value.items() if k != "__dc__"})
        return {k: decode(v) for k, v in value.items()}
    return value


def clean(value: Any, secrets: Iterable[str] = ()) -> Any:
    """`value` as JSON data with every secret out: the strings are scrubbed (keys, tokens, account parameters, the `secrets` given), and
    a key that names a secret keeps only a mask."""
    return _clean_data(encode(value), tuple(secrets))


def _clean_data(data: Any, known: tuple[str, ...]) -> Any:
    def walk(item: Any) -> Any:
        if isinstance(item, str):
            return scrub_text(_BLOB.sub(lambda m: f"[{len(m.group())} unbroken characters omitted]", item), known)
        if isinstance(item, list):
            return [walk(v) for v in item]
        if isinstance(item, dict):
            return {k: (MASK if _SECRET_KEY.search(k) else walk(v)) for k, v in item.items()}
        return item

    return walk(data)


def dumps(value: Any, secrets: Iterable[str] = (), *, limit: int = MAX_RECORD_CHARS) -> str:
    data = encode(value)
    if len(json.dumps(data, ensure_ascii=False, separators=(",", ":"))) > limit:          # the size is checked before the slow part
        raise RunError(f"a record is at most {limit} characters")
    body = json.dumps(_clean_data(data, tuple(secrets)), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    if len(body) > limit:
        raise RunError(f"a record is at most {limit} characters")
    return body


# --------------------------------------------------------------------------------------------------------- the records

def _content(entries: Any, outputs: Any, decisions: Sequence[Any], order_ids: Sequence[Any], analyst_seconds: Mapping[str, Any] | None,
             line: str | None) -> dict[str, Any]:
    if len(decisions) > MAX_LIST or len(order_ids) > MAX_LIST:
        raise RunError(f"a record holds at most {MAX_LIST} decisions and {MAX_LIST} order ids")
    ids = []
    for oid in order_ids:
        if isinstance(oid, bool) or not isinstance(oid, (int, str)) or len(str(oid)) > 64 or not str(oid):
            raise RunError("an order id is a short text or a whole number")
        ids.append(oid)
    body: dict[str, Any] = {"entries": entries if entries is not None else {}, "outputs": outputs if outputs is not None else {},
                            "decisions": list(decisions), "order_ids": ids}
    body["timings"] = timing.flag(analyst_seconds, line=line) if analyst_seconds is not None else None
    return body


def _check_identity(kind: str, signature: str) -> None:
    if not isinstance(kind, str) or not KIND_RE.match(kind):
        raise RunError("kind is 1 to 40 characters: a lower case letter, then letters, digits, hyphens or underscores")
    if not isinstance(signature, str) or not 1 <= len(signature) <= 128:
        raise RunError("signature is 1 to 128 characters")


def record(desk_id: int | None, kind: str, *, signature: str, entries: Any = None, outputs: Any = None, decisions: Sequence[Any] = (),
           order_ids: Sequence[Any] = (), status: str = "done", analyst_seconds: Mapping[str, Any] | None = None, line: str | None = None,
           started_at: Any = None, ended_at: Any = None, secrets: Iterable[str] = (), db: Database | None = None) -> int:
    """One finished cycle as one row: what came in, what came out, the decisions and the ids of the orders. Returns the row id."""
    _check_identity(kind, signature)
    if status not in FINAL_STATUSES:
        raise RunError(f"a record is written as {' or '.join(FINAL_STATUSES)}; a run that is still going uses begin()")
    body = dumps(_content(entries, outputs, decisions, order_ids, analyst_seconds, line), secrets)
    now = utcnow()
    with (db or get_database()).write() as u:
        return u.session.insert("run_records", {"desk_id": desk_id, "kind": kind, "signature": signature, "status": status, "record_json": body,
                                                "started_at": to_db_time(started_at) or now, "ended_at": to_db_time(ended_at) or now})


def begin(desk_id: int | None, kind: str, *, signature: str, started_at: Any = None, entries: Any = None,
          guard: Callable[[Any], None] | None = None, db: Database | None = None) -> int:
    """Open the row of a long run. It is `running` until `finish`; its checkpoints hang from it.

    `entries` is what the run is about (the asset), saved at once so a reader sees it before the first checkpoint exists. `guard(unit)` runs in the
    same write transaction as the insert: it may read the other runs and raise to refuse this one, so two openers never both pass the same limit."""
    _check_identity(kind, signature)
    body = dumps(_content(entries, None, (), (), None, None)) if entries is not None else "{}"
    with (db or get_database()).write() as u:
        if guard is not None:
            if u.session.engine == "postgres":
                # SQLite's write unit is BEGIN IMMEDIATE: one writer at a time. Postgres has no such lock, so two processes could both count
                # the same open runs and both pass. A transaction lock, released at commit, makes the count and the insert one step.
                u.session.execute("SELECT pg_advisory_xact_lock(:k)", {"k": BEGIN_LOCK})
            guard(u)
        return u.session.insert("run_records", {"desk_id": desk_id, "kind": kind, "signature": signature, "status": "running", "record_json": body,
                                                "started_at": to_db_time(started_at) or utcnow()})


def _transition(run_id: int, to: str, allowed: tuple[str, ...], values: dict[str, Any], db: Database | None) -> None:
    sets = ", ".join(f"{k} = :{k}" for k in ("status", *values))
    marks = ", ".join(f":s{i}" for i in range(len(allowed)))
    params = {"status": to, "id": run_id, **values, **{f"s{i}": s for i, s in enumerate(allowed)}}
    with (db or get_database()).write() as u:
        # The only statements that change a run row: the guarded step of its life (running, interrupted, then once done or failed).
        changed = u.session.execute(f"UPDATE run_records SET {sets} WHERE id = :id AND status IN ({marks})", params)
    if changed != 1:
        raise RunClosed(f"run {run_id} is not in a state that allows {to}")


def finish(run_id: int, status: str = "done", *, entries: Any = None, outputs: Any = None, decisions: Sequence[Any] = (),
           order_ids: Sequence[Any] = (), analyst_seconds: Mapping[str, Any] | None = None, line: str | None = None, ended_at: Any = None,
           secrets: Iterable[str] = (), db: Database | None = None) -> None:
    """Close a running run into its record. Once, and only from `running`: a finished record is never rewritten."""
    if status not in FINAL_STATUSES:
        raise RunError(f"a run finishes as {' or '.join(FINAL_STATUSES)}")
    body = dumps(_content(entries, outputs, decisions, order_ids, analyst_seconds, line), secrets)
    _transition(run_id, status, ("running",), {"record_json": body, "ended_at": to_db_time(ended_at) or utcnow()}, db)


def interrupt(run_id: int, *, db: Database | None = None) -> None:
    _transition(run_id, "interrupted", ("running",), {"ended_at": utcnow()}, db)


def resume(run_id: int, *, db: Database | None = None) -> None:
    """An interrupted run goes on from its checkpoint."""
    _transition(run_id, "running", ("interrupted",), {"ended_at": None}, db)


def get(run_id: int, *, db: Database | None = None) -> dict[str, Any] | None:
    with (db or get_database()).read() as u:
        row = u.session.one("SELECT * FROM run_records WHERE id = :i", {"i": run_id})
    return None if row is None else {**row, "record": json.loads(row["record_json"])}


def list_runs(desk_id: int | None = None, *, kind: str | None = None, status: str | None = None, limit: int = 50,
              db: Database | None = None) -> list[dict[str, Any]]:
    sql, params = "SELECT * FROM run_records WHERE 1 = 1", {"n": max(1, min(int(limit), 500))}
    for column, value in (("desk_id", desk_id), ("kind", kind), ("status", status)):
        if value is not None:
            sql, params[column] = f"{sql} AND {column} = :{column}", value
    with (db or get_database()).read() as u:
        rows = u.session.query(f"{sql} ORDER BY id DESC LIMIT :n", params)
    return [{**r, "record": json.loads(r["record_json"])} for r in rows]


# ----------------------------------------------------------------------------------------------------------- the files

def _day(value: Any) -> str:
    out = value.isoformat()[:10] if isinstance(value, (date, datetime)) else str(value)
    if not _DAY.match(out):
        raise RunError("a date is YYYY-MM-DD")
    date.fromisoformat(out)
    return out


def write_receipt(desk_id: int, receipt: Mapping[str, Any], *, run_id: int | None = None, secrets: Iterable[str] = (),
                  now: datetime | None = None) -> Path:
    """The JSON receipt of a run, in `receipts/` of the folder of the desk. The file is created, never opened over: a second run gets a
    second name even in the same second."""
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    body = dumps({"version": 1, "desk_id": desk_id, "run_id": run_id, "written_at": stamp.strftime("%Y-%m-%d %H:%M:%S"), "receipt": receipt}, secrets)
    folder = paths.ensure_dir(paths.desk_dir(desk_id) / RECEIPTS_DIR)
    for _ in range(16):
        target = folder / f"receipt-{stamp.strftime('%Y%m%dT%H%M%S')}-{_random.token_hex(4)}.json"
        try:
            with open(target, "x", encoding="utf-8") as handle:
                handle.write(body)
        except FileExistsError:
            continue
        return target
    raise RunError("a free receipt name was not found")


def _state_path(desk_id: int, day: str) -> Path:
    return paths.desk_dir(desk_id) / STATE_DIR / f"{day}.json"


def state_log(desk_id: int, symbol: str, day: Any, state: Mapping[str, Any], *, secrets: Iterable[str] = ()) -> Path:
    """The whole trace of a cycle in the JSON file of its date: one file per date, one entry per symbol (a later cycle of the same symbol
    and date replaces its own entry). `read_state_log` gives the same state back."""
    key, name = safe_ticker_component(symbol), _day(day)
    target = _state_path(desk_id, name)
    entry = clean(state, secrets)
    with _LOCK:
        paths.ensure_dir(target.parent)
        try:
            current = json.loads(target.read_text(encoding="utf-8"))
            states = current["states"] if current.get("version") == 1 and isinstance(current.get("states"), dict) else {}
        except (OSError, ValueError):
            states = {}
        states[key] = entry
        body = json.dumps({"version": 1, "date": name, "states": states}, sort_keys=True, ensure_ascii=False, indent=1)
        if len(body) > MAX_RECORD_CHARS * 4:
            raise RunError("the state log of one date is too large")
        handle, tmp = tempfile.mkstemp(dir=target.parent, prefix=".state.", suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                out.write(body)
            os.replace(tmp, target)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    return target


def read_state_log(desk_id: int, symbol: str, day: Any) -> dict[str, Any] | None:
    key, target = safe_ticker_component(symbol), _state_path(desk_id, _day(day))
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    found = data.get("states", {}).get(key) if isinstance(data, dict) else None
    return None if found is None else decode(found)


__all__ = ["RunClosed", "RunError", "begin", "clean", "decode", "dumps", "encode", "finish", "get", "interrupt", "list_runs", "read_state_log",
           "record", "resume", "state_log", "write_receipt"]
