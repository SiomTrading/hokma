"""Bugs 1093 (uv's minor version junction), 1094 (two truststore patches) and 1095 (chat failures hidden behind
"Connected" and a generic text)."""
import asyncio
import os
import ssl
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from hokma.core import pyhome
from hokma.update import runner
from hokma.update.source import UpdateError
from hokma.voice.model import ModelManager, classify_download_error

INSTALLER = Path(__file__).resolve().parents[2] / "installer"
LINK = "cpython-3.12-windows-x86_64-none"


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return tmp_path / "data"


def _python(root: Path, patch: int, windows: bool = True) -> Path:
    folder = root / f"cpython-3.12.{patch}-windows-x86_64-none"
    exe = folder / ("python.exe" if windows else "bin/python3")
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    return exe


# ------------------------------------------------------------------ 1093: the real folder, never the link

def test_real_python_takes_the_newest_patch_and_ignores_the_minor_link(tmp_path):
    root = tmp_path / "python"
    _python(root, 9)
    newest = _python(root, 13)
    (root / LINK).mkdir()                                         # stands for uv's junction
    assert pyhome.real_python(root, "3.12", windows=True) == newest
    assert pyhome.real_python(root, "3.12.13", windows=True) == newest
    assert pyhome.real_python(root, "3.11", windows=True) is None
    assert pyhome.real_python(tmp_path / "missing", "3.12", windows=True) is None


def test_real_python_skips_a_patch_folder_that_is_a_link(tmp_path):
    root = tmp_path / "python"
    real = _python(root, 13)
    link = root / "cpython-3.12.99-windows-x86_64-none"
    try:
        link.symlink_to(real.parent, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("no symlinks here")
    assert pyhome.real_python(root, "3.12", windows=True) == real


def test_drop_links_removes_only_the_link_not_the_folder_it_points_to(tmp_path):
    root = tmp_path / "python"
    real = _python(root, 13)
    link = root / LINK
    try:
        link.symlink_to(real.parent, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("no symlinks here")
    assert pyhome.drop_links(root) == [link]
    assert not os.path.lexists(link) and real.is_file()
    assert pyhome.drop_links(root) == []


def test_drop_links_survives_a_path_windows_refuses(tmp_path, monkeypatch):
    root = tmp_path / "python"
    (root / LINK).mkdir(parents=True)

    def refuse(self):
        raise OSError(448, "untrusted mount point")

    monkeypatch.setattr(Path, "is_symlink", refuse)
    assert pyhome.is_link(root / LINK) is True                    # unreadable counts as a link
    pyhome.drop_links(root)                                       # no crash


def test_the_updater_builds_the_venv_from_the_real_python_on_windows(data, monkeypatch):
    root = data / "python"
    exe = _python(root, 13)
    (root / LINK).mkdir()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(runner, "_run", lambda *a, **k: "")
    monkeypatch.setattr(pyhome, "is_link", lambda p: p.name == LINK)
    removed = []
    monkeypatch.setattr(os, "rmdir", lambda p: removed.append(Path(p).name))
    assert runner._install_python("3.12", {}) == str(exe)
    assert removed == [LINK]


def test_the_updater_goes_on_when_uv_failed_only_on_the_link_and_the_real_python_is_there(data, monkeypatch):
    exe = _python(data / "python", 13)
    monkeypatch.setattr(sys, "platform", "win32")

    def fail(*a, **k):
        raise UpdateError("Failed to create Python minor version link directory (os error 448)")

    monkeypatch.setattr(runner, "_run", fail)
    assert runner._install_python("3.12", {}) == str(exe)


def test_the_updater_still_fails_when_uv_failed_and_there_is_no_python(data, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")

    def fail(*a, **k):
        raise UpdateError("boom")

    monkeypatch.setattr(runner, "_run", fail)
    with pytest.raises(UpdateError):
        runner._install_python("3.12", {})


def test_the_updater_does_not_touch_other_systems(data, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(runner, "_run", lambda *a, **k: "")
    assert runner._install_python("3.12", {}) == "3.12"


def test_the_windows_installer_uses_the_real_python_and_drops_the_junction():
    install = (INSTALLER / "install.ps1").read_text()
    native = (INSTALLER / "native.ps1").read_text()
    assert "function Get-RealPython" in native and "function Remove-PythonLinks" in native
    assert "ReparsePoint" in native and "[System.IO.Directory]::Delete($d.FullName, $false)" in native
    assert install.index("Get-RealPython $PyRoot $PyVersion") < install.index("Remove-PythonLinks $PyRoot")
    assert install.index("Remove-PythonLinks $PyRoot") < install.index('"--python", $PySpec')
    assert '"--python", $PyVersion' not in install


# ------------------------------------------------------------------ 1094: two truststore patches

def test_a_recursion_error_in_the_download_gets_its_own_clear_code():
    assert classify_download_error(RecursionError("maximum recursion depth exceeded")) == "tls_conflict"
    wrapped = OSError("x")
    wrapped.__cause__ = RecursionError("maximum recursion depth exceeded")
    assert classify_download_error(wrapped) == "tls_conflict"


def test_the_download_reports_tls_conflict_not_a_raw_recursion_error(data):
    def boom(repo, target, report):
        raise RecursionError("maximum recursion depth exceeded")

    manager = ModelManager("small", fetch=boom)
    manager.start_download()
    manager.wait(10)
    snap = manager.snapshot()
    assert snap["state"] == "error" and snap["error_code"] == "tls_conflict"


def test_undo_stacked_truststore_restores_ssl_and_is_a_no_op_otherwise():
    truststore = pytest.importorskip("truststore")
    from hokma.core.tls import undo_stacked_truststore

    original = ssl.SSLContext
    try:
        assert undo_stacked_truststore() is False                 # nothing injected
        truststore.inject_into_ssl()                              # what pip_system_certs does at startup
        assert ssl.SSLContext is truststore.SSLContext
        assert undo_stacked_truststore() is True
        assert ssl.SSLContext is original
        ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)      # what httpcore2 builds: no recursion
        assert ctx.verify_mode is not None
    finally:
        truststore.extract_from_ssl()


def test_the_download_undoes_the_stacked_patch_before_it_connects(monkeypatch, tmp_path):
    seen = []
    import hokma.core.tls as tls

    monkeypatch.setattr(tls, "undo_stacked_truststore", lambda: seen.append("undone") or True)
    fake = SimpleNamespace(snapshot_download=lambda *a, **k: seen.append("download"))
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake)
    ModelManager._fetch_from_hub("Systran/faster-whisper-small", tmp_path, lambda *a, **k: None)
    assert seen == ["undone", "download"]


def test_the_lock_leaves_pip_system_certs_out_of_every_platform():
    root = Path(__file__).resolve().parents[2]
    text = (root / "uv.lock").read_text()
    assert "pip-system-certs" in (root / "pyproject.toml").read_text()
    assert 'overrides = [{ name = "pip-system-certs", marker = "sys_platform == \'never\'" }]' in text
    assert '{ name = "pip-system-certs", marker = "sys_platform == \'win32\'" }' not in text


# ------------------------------------------------------------------ 1095: the chat says why, and the status does not lie

def test_the_engine_reports_env_failed_with_the_real_cause(monkeypatch):
    from hokma.engines import claude_code
    from hokma.engines.base import EngineContext, EngineError

    def broken():
        raise OSError(448, "The path cannot be traversed because it contains an untrusted mount point")

    monkeypatch.setattr(claude_code, "filtered_env", broken)
    owner = SimpleNamespace(launch=lambda: SimpleNamespace(env={}, args=[]), claude_bin="claude", session_token="")
    ctx = EngineContext(conversation_id="1", model="haiku", cwd=".", mode="ask")
    with pytest.raises(EngineError) as info:
        asyncio.run(claude_code.ClaudeCodeAdapter(owner).open(ctx))
    assert info.value.code == "env_failed" and "448" in info.value.message


def test_the_chat_shows_the_engine_message_for_every_code_that_does_not_explain_itself():
    root = Path(__file__).resolve().parents[2] / "dashboard" / "src"
    state = (root / "screens/chat/state.ts").read_text()
    api = (root / "screens/chat/chatApi.ts").read_text()
    messages = (root / "i18n/messages/chat.ts").read_text()
    assert "!SELF_EXPLAINED.has(event.code)" in state and "DETAILED_ERRORS" not in state
    for code in ("env_failed", "instructions", "auth", "region", "quota", "network", "no_connection", "no_model",
                 "connection_removed", "unsupported_engine", "codex_not_found", "gemini_not_found"):
        assert f'"{code}"' in api, code
        assert messages.count(f'"chat.err.{code}"') == 2, code            # es and en


# ------------------------------------------------------------------ Vera round 1

def test_uv_is_not_asked_to_install_a_python_that_is_already_there_on_windows(data, monkeypatch):
    """uv makes the minor version link again on every `uv python install`, even without downloading anything."""
    exe = _python(data / "python", 13)
    monkeypatch.setattr(sys, "platform", "win32")
    calls = []
    monkeypatch.setattr(runner, "_run", lambda *a, **k: calls.append(a) or "")
    assert runner._install_python("3.12", {}) == str(exe)
    assert calls == []


def test_uv_is_asked_when_the_python_is_missing_and_always_on_other_systems(data, monkeypatch):
    calls = []
    monkeypatch.setattr(runner, "_run", lambda *a, **k: calls.append(a) or "")
    monkeypatch.setattr(sys, "platform", "win32")
    runner._install_python("3.12", {})
    monkeypatch.setattr(sys, "platform", "linux")
    _python(data / "python", 13, windows=False)
    runner._install_python("3.12", {})
    assert len(calls) == 2


def test_the_installer_asks_uv_only_when_no_real_python_is_there_and_only_removes_cpython_links():
    install = (INSTALLER / "install.ps1").read_text()
    native = (INSTALLER / "native.ps1").read_text()
    assert "if (-not $PyExe) { $code = Invoke-Native $Uv @(\"python\", \"install\", $PyVersion) }" in install
    assert '$d.Name -like "cpython-*" -and ($d.Attributes -band [System.IO.FileAttributes]::ReparsePoint)' in native


def test_an_engine_error_without_a_message_names_its_code_and_engine_log():
    from hokma.engine.session import ChatSession as owner

    text = owner._error_text({"type": "error", "code": "auth"})
    assert "auth" in text and "engine.log" in text
    assert owner._error_text({"code": "engine_error", "message": "  real cause  "}) == "real cause"
