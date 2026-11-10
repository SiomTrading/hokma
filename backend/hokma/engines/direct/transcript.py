"""The history of a direct-engine conversation: one JSON object per line in <data>/engine/direct/<id>.jsonl.
The API is stateless, so Hokma keeps the whole exchange (tool calls and results included) here."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from hokma.core.config import engine_dir

KEEP_LAST = 400


def path_for(key: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", key)[:80] or "x"
    return engine_dir() / "direct" / f"{safe}.jsonl"


def load(key: str) -> list[dict[str, Any]]:
    try:
        lines = path_for(key).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-KEEP_LAST:]:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and item.get("role") in ("user", "assistant", "tool"):
            out.append(item)
    while out and out[0]["role"] != "user":           # never start in the middle of a tool exchange
        out.pop(0)
    return out


def append(key: str, message: dict[str, Any]) -> None:
    p = path_for(key)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
