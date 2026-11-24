"""Where the IPS lives and how it is versioned (SPEC-024).

The IPS is two files in the project of kind 'ips': ips.json (the data, with the contract of limits the desks read) and IPS.md (the
readable text, generated from it). Every save is a commit in the repository of that project: 'IPS draft: {block}' for a draft,
'IPS v{N}: {blocks}' for an adopted version. Results, limits and checks are always recalculated here; whatever the caller sends
for them is never read.
"""
from __future__ import annotations

import datetime
import json
import threading
from pathlib import Path
from typing import Any

from hokma.core.config import hokma_home
from hokma.ips import document, method
from hokma.kit import safefs
from hokma.projects import gitops

JSON_FILE = "ips.json"
DOC_FILE = "IPS.md"
IPS_KIND = "ips"
MAX_JSON_BYTES = 400_000
_LOCK = threading.RLock()

# the answers a block is made of, to name what changed when the caller does not
BLOCK_OF_KEY = {"profile": "profile", "assets": "assets", "has_debts": "debts", "debts": "debts", "cashflow": "cashflow",
                "income": "income", "conduct": "conduct", "prop": "prop", "pockets": "pockets", "objectives": "objectives",
                "restrictions": "restrictions", "declared_limits": "restrictions"}


class IpsStoreError(ValueError):
    """The IPS cannot be read or written. The message is shown as written."""


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def project_folder(*, create: bool) -> Path | None:
    """Folder of the project of kind 'ips'. With create=True the project is made when there is none."""
    from hokma.db import get_database
    from hokma.projects import service

    database = get_database()
    with database.read() as u:
        found = u.projects.list(kind=IPS_KIND)
    if found:
        folder = Path(found[0].path)
    elif create:
        folder = Path(service.create_project("IPS", IPS_KIND, db=database)["path"])
    else:
        return None
    root = Path(hokma_home()).resolve()
    try:
        folder.resolve().relative_to(root)
    except ValueError:
        raise IpsStoreError("The folder of the IPS project is outside the working folder.") from None
    if folder.is_symlink() or not folder.is_dir():
        raise IpsStoreError("The folder of the IPS project is not a plain folder.")
    return folder


def read_data(folder: Path) -> dict | None:
    """ips.json parsed, None when there is none yet."""
    try:
        raw = safefs.read_regular(folder, JSON_FILE, max_bytes=MAX_JSON_BYTES, single_link=True)
    except (safefs.UnsafePath, OSError):
        raise IpsStoreError("ips.json is not a plain file inside the IPS project.") from None
    if raw is None:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise IpsStoreError("ips.json is not valid JSON.") from None
    if not isinstance(data, dict) or data.get("schema") != method.SCHEMA:
        raise IpsStoreError("ips.json does not follow the Hokma IPS format.")
    return data


def derive_changed(patch: dict) -> list[str]:
    seen: list[str] = []
    for key in patch:
        block = BLOCK_OF_KEY.get(key)
        if block and block not in seen:
            seen.append(block)
    return seen


def _blocks(changed: Any, patch: dict) -> list[str]:
    if changed is None:
        return derive_changed(patch)
    if isinstance(changed, str):
        changed = [x for x in (p.strip() for p in changed.split(",")) if x]
    if not isinstance(changed, list):
        raise IpsStoreError("changed: a list of block names.")
    bad = [x for x in changed if x not in method.METHOD_TABLES["blocks"]]
    if bad:
        raise IpsStoreError(f"changed: unknown block {', '.join(map(str, bad))}. Blocks: {', '.join(method.METHOD_TABLES['blocks'])}.")
    return list(dict.fromkeys(changed))


def build(inputs: dict, *, version: int, status: str, created_at: str, updated_at: str, history: list[dict]) -> dict:
    """The ips.json document of clean inputs, calculated here."""
    calc = method.compute(inputs)
    return {"schema": method.SCHEMA, "version": version, "status": status, "created_at": created_at, "updated_at": updated_at,
            "currency": method.CURRENCY, "inputs": inputs, "results": calc["results"], "limits": calc["limits"],
            "checks": calc["checks"], "missing": calc["missing"], "history": history}


def save(partial: Any, *, mode: str, fx: Any = None, changed: Any = None, note: str | None = None) -> dict:
    """Save a draft or adopt the IPS. 'partial' holds the blocks the owner just answered; they go over the stored ones."""
    if mode not in ("draft", "adopt"):
        raise IpsStoreError("mode: draft or adopt.")
    with _LOCK:
        patch = method.to_usd(partial, fx)
        folder = project_folder(create=False)
        current = read_data(folder) if folder else None
        merged = method.merge(current["inputs"] if current else None, patch)
        clean, warnings = method.normalize(merged)                 # nothing is created or written when an answer is not valid
        if current is None and not patch:
            raise IpsStoreError("There is nothing to save: send the answers of at least one block.")
        blocks = _blocks(changed, patch)
        folder = folder or project_folder(create=True)
        stamp = _now().replace(microsecond=0).isoformat().replace("+00:00", "Z")
        previous = int(current["version"]) if current else 0
        history = list(current["history"]) if current else []
        created = current["created_at"] if current else stamp
        if mode == "adopt":
            version = previous + 1
            history.append({"version": version, "date": stamp[:10], "changed": blocks,
                            "note": method._text(note, "note") or ""})
            message = f"IPS v{version}: {', '.join(blocks) if blocks else 'draft adopted'}"
        else:
            version = previous
            message = f"IPS draft: {', '.join(blocks) if blocks else 'draft'}"
        data = build(clean, version=version, status="adopted" if mode == "adopt" else "draft", created_at=created,
                     updated_at=stamp, history=history)
        safefs.write_atomic(folder, JSON_FILE, (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
        safefs.write_atomic(folder, DOC_FILE, document.render(data).encode("utf-8"))
        done = gitops.commit(folder, [JSON_FILE, DOC_FILE], message)
        return {"saved": True, "status": data["status"], "version": version, "commit": message,
                "committed": done.ok, "commit_problem": None if done.ok else done.reason,
                "files": [JSON_FILE, DOC_FILE], "warnings": warnings, "results": data["results"], "limits": data["limits"],
                "checks": data["checks"], "missing": data["missing"],
                **({"closing": method.CLOSING} if mode == "adopt" else {})}
