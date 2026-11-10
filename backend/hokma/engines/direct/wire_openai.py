"""Chat Completions with streaming and tool calls (OpenAI and every compatible endpoint)."""
from __future__ import annotations

import json
from typing import Any, Iterator

from hokma.engines.direct.wire import Stream


def messages(system: str, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": system}] if system else []
    for m in history:
        if m["role"] == "user":
            out.append({"role": "user", "content": m["text"]})
        elif m["role"] == "assistant":
            msg: dict[str, Any] = {"role": "assistant", "content": m.get("text") or None}
            if m.get("calls"):
                msg["tool_calls"] = [{"id": c["id"], "type": "function",
                                      "function": {"name": c["name"], "arguments": json.dumps(c["input"])}}
                                     for c in m["calls"]]
            out.append(msg)
        else:
            for r in m["results"]:
                out.append({"role": "tool", "tool_call_id": r["id"], "content": r["text"]})
    return out


def open_stream(base_url: str, key: str | None, model: str, system: str, history: list[dict[str, Any]],
                tools: list[dict[str, Any]]) -> Stream:
    body: dict[str, Any] = {"model": model, "messages": messages(system, history), "stream": True,
                            "stream_options": {"include_usage": True}}
    if tools:
        body["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                           "parameters": t["schema"]}} for t in tools]
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return Stream(base_url.rstrip("/") + "/chat/completions", headers, body)


def parse(lines: Iterator[str]) -> Iterator[tuple[str, Any]]:
    calls: dict[int, dict[str, Any]] = {}
    for data in lines:
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except ValueError:
            continue
        usage = chunk.get("usage")
        if usage:
            details = usage.get("prompt_tokens_details") or {}
            yield "usage", {"tokens_in": int(usage.get("prompt_tokens") or 0),
                            "tokens_out": int(usage.get("completion_tokens") or 0),
                            "cache_read": int(details.get("cached_tokens") or 0), "cost_usd": 0.0}
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                yield "text", delta["content"]
            for tc in delta.get("tool_calls") or []:
                slot = calls.setdefault(int(tc.get("index", 0)), {"id": "", "name": "", "args": ""})
                slot["id"] = tc.get("id") or slot["id"]
                fn = tc.get("function") or {}
                slot["name"] = fn.get("name") or slot["name"]
                slot["args"] += fn.get("arguments") or ""
    for index in sorted(calls):
        c = calls[index]
        try:
            parsed = json.loads(c["args"] or "{}")
        except ValueError:
            parsed = {}
        yield "call", {"id": c["id"] or f"call_{index}", "name": c["name"],
                       "input": parsed if isinstance(parsed, dict) else {}}
