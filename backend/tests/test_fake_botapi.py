"""The Bot API double (tests/support/fake_botapi) starts on a free loopback port and answers getMe."""
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

from support.fake_botapi.botapi import VALID

TESTS = Path(__file__).resolve().parent


def test_the_double_answers_getme_on_a_free_loopback_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    proc = subprocess.Popen([sys.executable, "-m", "support.fake_botapi.botapi", str(port)], cwd=TESTS,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        base, token = f"http://127.0.0.1:{port}", next(iter(VALID))
        deadline = time.monotonic() + 20
        while True:
            try:
                reply = httpx.get(f"{base}/bot{token}/getMe", timeout=2)
                break
            except httpx.TransportError:
                assert time.monotonic() < deadline and proc.poll() is None, "the double did not start"
                time.sleep(0.2)
        assert reply.json() == {"ok": True, "result": {"id": 111, "is_bot": True, "first_name": "B", "username": "bot1"}}
        assert httpx.get(f"{base}/bot999:nope/getMe", timeout=2).status_code == 401
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_hokma_connects_to_the_double_through_the_api_base_variable(tmp_path, monkeypatch):
    import asyncio

    from hokma.db import get_database, migrate
    from hokma.telegram.glue import TOKEN_ENTRY, TelegramGlue
    from test_telegram_glue import MemoryKeys

    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()
    migrate()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setenv("HOKMA_TELEGRAM_API_BASE", f"http://127.0.0.1:{port}")
    proc = subprocess.Popen([sys.executable, "-m", "support.fake_botapi.botapi", str(port)], cwd=TESTS,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                httpx.get(f"http://127.0.0.1:{port}/_ctl/state", timeout=2)
                break
            except httpx.TransportError:
                assert time.monotonic() < deadline and proc.poll() is None, "the double did not start"
                time.sleep(0.2)
        db = get_database()
        glue = TelegramGlue(lambda: db, lambda: None, keys=MemoryKeys(), data_dir=tmp_path / "pairing")
        glue.keys.set(TOKEN_ENTRY, next(iter(VALID)))

        async def connect():
            assert await glue.start() and glue.link.connected
            await glue.stop()

        asyncio.run(connect())
        calls = [c[0] for c in httpx.get(f"http://127.0.0.1:{port}/_ctl/state", timeout=2).json()["calls"]]
        assert "getMe" in calls and "getUpdates" in calls
    finally:
        proc.terminate()
        proc.wait(timeout=10)
