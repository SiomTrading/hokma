"""Task 3171: 0.0.22 broke the chat on real Windows. What can be simulated on Linux is simulated here: the cp1252 pipes
of a `python -I` process (hooks and Hokma's MCP server), the `st_nlink == 0` that `DirEntry.stat()` gives on Windows,
a PATH entry that makes the path comparison fail, and a failure while starting the engine, which must reach the chat
with its real text and land in engine.log."""
import io
import json
import os
import sys
from types import SimpleNamespace

import pytest

from engine_support import connect, env, hard_timeout, make_client, posix_only, turn  # noqa: F401
from hokma.kit import guard, hardblock, mirror, trash_mcp

SKILLS = "skills"


def _cp1252_stdin(text: str) -> io.TextIOWrapper:
    """What a Windows `python -I` sees on its stdin: UTF-8 bytes behind a cp1252 text layer."""
    return io.TextIOWrapper(io.BytesIO(text.encode("utf-8")), encoding="cp1252")


def _cp1252_stdout() -> io.TextIOWrapper:
    return io.TextIOWrapper(io.BytesIO(), encoding="cp1252")


# ------------------------------------------------------------------ hooks: UTF-8 payload whatever the code page

def test_hardblock_reads_a_utf8_payload_behind_a_cp1252_pipe(tmp_path, monkeypatch):
    work = tmp_path / "JOS\u00c9 \u00d1and\u00fa \u00cddolo"            # \u00c9 = C3 89, \u00cd = C3 8D: 0x8D does not decode as cp1252
    work.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    target = work / "nota.md"
    target.write_text("hola", encoding="utf-8")
    payload = {"tool_name": "Read", "tool_input": {"file_path": str(target)}, "cwd": str(work)}
    monkeypatch.setattr(sys, "stdin", _cp1252_stdin(json.dumps(payload, ensure_ascii=False)))
    monkeypatch.setattr(sys, "stderr", _cp1252_stdout())
    code = hardblock.main(["--data", str(data), "--workdir", str(work), "--protect", str(tmp_path / "pkg")])
    assert code == 0


def test_hardblock_write_inside_a_folder_with_accents_is_not_outside(tmp_path, monkeypatch):
    work = tmp_path / "Usuarios" / "Jos\u00e9"
    work.mkdir(parents=True)
    data = tmp_path / "data"
    data.mkdir()
    payload = {"tool_name": "Write", "tool_input": {"file_path": str(work / "plan.md"), "content": "x"},
               "cwd": str(work)}
    monkeypatch.setattr(sys, "stdin", _cp1252_stdin(json.dumps(payload, ensure_ascii=False)))
    monkeypatch.setattr(sys, "stderr", _cp1252_stdout())
    assert hardblock.main(["--data", str(data), "--workdir", str(work), "--protect", str(tmp_path / "pkg")]) == 0


def test_hardblock_still_fails_closed_and_says_why(tmp_path, monkeypatch):
    err = _cp1252_stdout()
    monkeypatch.setattr(sys, "stdin", _cp1252_stdin("not json"))
    monkeypatch.setattr(sys, "stderr", err)
    assert hardblock.main(["--data", str(tmp_path), "--workdir", str(tmp_path), "--protect", str(tmp_path)]) == 2
    err.flush()
    text = err.buffer.getvalue().decode("utf-8")
    assert "HOKMA_BLOCKED error" in text and "JSONDecodeError" in text


def test_guard_reads_a_utf8_payload_behind_a_cp1252_pipe(tmp_path, monkeypatch):
    work = tmp_path / "\u00cddolo"
    work.mkdir()
    payload = {"tool_name": "Write", "tool_input": {"file_path": str(work / "a.md"), "content": "\u2192"}, "cwd": str(work)}
    monkeypatch.setattr(sys, "stdin", _cp1252_stdin(json.dumps(payload, ensure_ascii=False)))
    monkeypatch.setattr(sys, "stderr", _cp1252_stdout())
    assert guard.main(["--protect", str(tmp_path / "pkg")]) == 0


# ------------------------------------------------------------------ Hokma's MCP server

def test_mcp_server_survives_non_cp1252_text_on_both_pipes(tmp_path, monkeypatch):
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18",
                                                                        "clientInfo": {"name": "\u00cddolo \u2192 \u2713"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "ping"},
    ]
    out = _cp1252_stdout()
    monkeypatch.setattr(sys, "stdin", _cp1252_stdin("\n".join(json.dumps(m, ensure_ascii=False) for m in lines) + "\n"))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", _cp1252_stdout())
    monkeypatch.chdir(tmp_path)
    assert trash_mcp.main(["--data", str(tmp_path), "--workdir", str(tmp_path)]) == 0
    out.flush()
    answers = [json.loads(line) for line in out.buffer.getvalue().decode("utf-8").splitlines()]
    assert [a["id"] for a in answers] == [1, 2, 3]
    assert all("error" not in a for a in answers)
    assert any(t["name"] == "trash_move" for t in answers[1]["result"]["tools"])


def test_mcp_reply_is_ascii_on_the_wire(monkeypatch):
    out = _cp1252_stdout()
    monkeypatch.setattr(sys, "stdout", out)
    trash_mcp.reply({"jsonrpc": "2.0", "id": 1, "result": {"text": "Conversaci\u00f3n \u2192 hecho \u2713 \u4f60\u597d"}})
    out.flush()
    raw = out.buffer.getvalue()
    assert raw.isascii() and json.loads(raw)["result"]["text"] == "Conversaci\u00f3n \u2192 hecho \u2713 \u4f60\u597d"


# ------------------------------------------------------------------ mirror: st_nlink is 0 on Windows

class _WinEntry:
    """A DirEntry as Windows gives it: `stat()` from the directory listing, `st_nlink` always 0."""

    def __init__(self, entry: os.DirEntry) -> None:
        self._e, self.name, self.path = entry, entry.name, entry.path

    def stat(self, follow_symlinks=True):
        st = self._e.stat(follow_symlinks=follow_symlinks)
        return SimpleNamespace(st_mode=st.st_mode, st_nlink=0, st_ino=0, st_dev=0, st_size=st.st_size)

    def is_dir(self, follow_symlinks=True):
        return self._e.is_dir(follow_symlinks=follow_symlinks)

    def is_symlink(self):
        return self._e.is_symlink()


def test_mirror_copies_agents_and_skills_when_direntry_has_no_link_count(tmp_path, monkeypatch):
    work, config = tmp_path / "work", tmp_path / "config"
    claude_dir = work / ".claude"
    (claude_dir / "agents").mkdir(parents=True)
    (claude_dir / SKILLS / "resumen").mkdir(parents=True)
    (claude_dir / "agents" / "analista.md").write_text("---\ndescription: d\n---\nbody", encoding="utf-8")
    (claude_dir / SKILLS / "resumen" / "SKILL.md").write_text("---\ndescription: s\n---\nx", encoding="utf-8")
    real_scandir = os.scandir

    class _Scan:
        def __init__(self, path):
            self._it = real_scandir(path)

        def __enter__(self):
            return [_WinEntry(e) for e in self._it]

        def __exit__(self, *exc):
            self._it.close()

    monkeypatch.setattr(mirror.os, "scandir", _Scan)
    result = mirror.sync(work, config)
    assert result == {"agents": ["analista"], "skills": ["resumen"]}
    assert (config / "agents" / "analista.md").is_file() and (config / SKILLS / "resumen" / "SKILL.md").is_file()


def test_mirror_still_refuses_a_hard_link(tmp_path):
    work, config = tmp_path / "work", tmp_path / "config"
    (work / ".claude" / "agents").mkdir(parents=True)
    secret = tmp_path / "secret.md"
    secret.write_text("---\ndescription: d\n---\nkey", encoding="utf-8")
    os.link(secret, work / ".claude" / "agents" / "x.md")
    assert mirror.sync(work, config)["agents"] == []


# ------------------------------------------------------------------ find_claude: fail open on what is not a lock

def test_trusted_path_keeps_an_entry_the_comparison_cannot_judge(tmp_path, monkeypatch):
    from hokma.engine import process
    from hokma.kit import guard as guard_mod

    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HOKMA_LOG_TEST", "1")
    bad = str(tmp_path / "Z-disconnected")

    real = guard_mod.under_any

    def flaky(path, roots):
        if path == bad:
            raise OSError(53, "The network path was not found")
        return real(path, roots)

    monkeypatch.setattr(guard_mod, "under_any", flaky)
    good = str(tmp_path / "bin")
    kept = process._trusted_path(os.pathsep.join([bad, good, "relative", f'"{good}2"'])).split(os.pathsep)
    assert kept == [bad, good, good + "2"]
    assert "Z-disconnected" in (tmp_path / "data" / "logs" / "engine.log").read_text(encoding="utf-8")


def test_trusted_path_still_drops_the_writable_data_folder(tmp_path, monkeypatch):
    from hokma.engine import process

    data = tmp_path / "data"
    monkeypatch.setenv("HOKMA_DATA_DIR", str(data))
    kept = process._trusted_path(os.pathsep.join([str(data / "plugins" / "x"), str(data / "bin"), "/usr/bin"]))
    assert kept.split(os.pathsep) == [str(data / "bin"), "/usr/bin"]


def test_find_claude_reads_a_path_key_in_any_case(tmp_path, monkeypatch):
    from hokma.engine import process

    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "data"))
    folder = tmp_path / "bin"
    folder.mkdir()
    exe = folder / "claude"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    assert process.find_claude({"Path": str(folder)}) == str(exe)


# ------------------------------------------------------------------ the chat shows the real error

@posix_only
def test_a_bug_while_starting_the_engine_reaches_the_chat_and_engine_log(env, monkeypatch):
    from hokma.engines import claude_code

    def broken(*_a, **_k):
        raise ValueError("path C:\\Users\\Jos\u00e9 is bad")

    monkeypatch.setattr(claude_code, "filtered_env", broken)
    with make_client(env.engine()) as client, connect(client) as ws:
        events = turn(ws, "hola")
    last = events[-1]
    assert last["type"] == "error" and last["code"] == "engine_error"
    assert "ValueError" in last["message"] and "Jos\u00e9" in last["message"]
    assert not any(e["type"] == "done" for e in events)          # no empty "done" before the error any more
    log = (env.data / "logs" / "engine.log").read_text(encoding="utf-8")
    assert "ValueError" in log and "Traceback" in log


@posix_only
def test_an_error_result_carries_its_text_and_is_logged(env):
    with make_client(env.engine()) as client, connect(client) as ws:
        events = turn(ws, "[error] hola")
    assert events[-1]["code"] == "engine_error" and "simulated failure" in events[-1]["message"]
    assert "simulated failure" in (env.data / "logs" / "engine.log").read_text(encoding="utf-8")


def test_hokma_entry_points_set_utf8_before_reading(monkeypatch):
    """Every `python -I` entry point of Hokma that talks over pipes switches them to UTF-8 first."""
    import inspect

    assert "utf8_stdio()" in inspect.getsource(guard.main)
    assert "guard.utf8_stdio()" in inspect.getsource(hardblock.main)
    assert "reconfigure" in inspect.getsource(trash_mcp.main)


if __name__ == "__main__":                                   # pragma: no cover
    raise SystemExit(pytest.main([__file__]))
