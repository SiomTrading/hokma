"""Messages API with streaming and tool_use (Anthropic API key, or a gateway that speaks the same protocol)."""
from __future__ import annotations

import json
from typing import Any, Iterator

from hokma.engines.direct.wire import Stream

VERSION = "2023-06-01"
MAX_TOKENS = 8192


def messages(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in history:
        if m["role"] == "user":
            out.append({"role": "user", "content": [{"type": "text", "text": m["text"]}]})
        elif m["role"] == "assistant":
            blocks: list[dict[str, Any]] = []
            if m.get("text"):
                blocks.append({"type": "text", "text": m["text"]})
            blocks += [{"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["input"]}
                       for c in m.get("calls") or []]
            out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": "..."}]})
        else:
            out.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": r["id"], "content": r["text"], "is_error": bool(r["is_error"])}
                for r in m["results"]]})
    return out


def open_stream(base_url: str, key: str | None, model: str, system: str, history: list[dict[str, Any]],
                tools: list[dict[str, Any]]) -> Stream:
    body: dict[str, Any] = {"model": model, "max_tokens": MAX_TOKENS, "stream": True, "messages": messages(history)}
    if system:
        body["system"] = system
    if tools:
        body["tools"] = [{"name": t["name"], "description": t["description"], "input_schema": t["schema"]}
                         for t in tools]
    headers = {"anthropic-version": VERSION, **({"x-api-key": key} if key else {})}
    return Stream(base_url.rstrip("/") + "/v1/messages", headers, body)


def parse(lines: Iterator[str]) -> Iterator[tuple[str, Any]]:
    blocks: dict[int, dict[str, Any]] = {}
    usage = {"tokens_in": 0, "tokens_out": 0, "cache_read": 0, "cost_usd": 0.0}
    for data in lines:
        try:
            ev = json.loads(data)
        except ValueError:
            continue
        kind = ev.get("type")
        if kind == "message_start":
            u = (ev.get("message") or {}).get("usage") or {}
            usage["tokens_in"] = int(u.get("input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0)
            usage["cache_read"] = int(u.get("cache_read_input_tokens") or 0)
        elif kind == "content_block_start":
            block = ev.get("content_block") or {}
            if block.get("type") == "tool_use":
                blocks[int(ev.get("index", 0))] = {"id": block.get("id", ""), "name": block.get("name", ""), "args": ""}
        elif kind == "content_block_delta":
            delta = ev.get("delta") or {}
            if delta.get("type") == "text_delta":
                yield "text", delta.get("text", "")
            elif delta.get("type") == "input_json_delta":
                slot = blocks.get(int(ev.get("index", 0)))
                if slot is not None:
                    slot["args"] += delta.get("partial_json", "")
        elif kind == "message_delta":
            usage["tokens_out"] = int((ev.get("usage") or {}).get("output_tokens") or usage["tokens_out"])
        elif kind == "error":
            raise RuntimeError(json.dumps(ev.get("error") or ev)[:500])
    for index in sorted(blocks):
        b = blocks[index]
        try:
            parsed = json.loads(b["args"] or "{}")
        except ValueError:
            parsed = {}
        yield "call", {"id": b["id"], "name": b["name"], "input": parsed if isinstance(parsed, dict) else {}}
    yield "usage", usage
