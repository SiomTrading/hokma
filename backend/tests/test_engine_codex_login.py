"""Codex device-code login: the link and the code are read from the CLI output, colors removed."""
import stat

from hokma.engines import codex_login


def test_device_login_reads_link_and_code(tmp_path, monkeypatch):
    fake = tmp_path / "codex"
    fake.write_text("#!/bin/sh\nprintf '1. Open\\n   \\033[94mhttps://auth.example/device\\033[0m\\n2. code\\n   \\033[94mAB12-CD34E\\033[0m\\n'\nsleep 0.3\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    monkeypatch.setenv("HOKMA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "data"))
    got = codex_login.start()
    assert got["url"] == "https://auth.example/device" and got["code"] == "AB12-CD34E"
    import time
    time.sleep(0.6)
    assert codex_login.status()["state"] == "done"
    codex_login.cancel()
    assert codex_login.status()["state"] == "idle"


def test_device_login_without_cli(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert codex_login.start()["state"] == "no_cli"


def test_start_creates_codex_home_before_login(tmp_path, monkeypatch):
    """Bug 1206: Codex fails when CODEX_HOME points to a folder that does not exist."""
    fake = tmp_path / "codex"
    fake.write_text("#!/bin/sh\n[ -d \"$CODEX_HOME\" ] && echo https://auth.example/d && echo AB12-CD34E || echo missing\nsleep 0.3\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "fresh"))
    from hokma.core.config import codex_home_dir
    assert not codex_home_dir().exists()
    got = codex_login.start()
    assert codex_home_dir().is_dir() and got["code"] == "AB12-CD34E"
    codex_login.cancel()
