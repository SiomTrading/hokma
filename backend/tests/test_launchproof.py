"""Task 3299, SPEC-015, bug 1228 (engine launch proof): 'hokma engine' serves only when the supervisor launched it.

An agent can run 'printf token | hokma engine --port N &' from its shell. That used to start a marked server whose HTTP
routes reach the broker, the orders and the owner. Now the engine needs the one-time proof the supervisor writes on the
engine's standard input and redeems on the control channel (core/launchproof.py). The subprocess tests start a REAL
engine the way an agent would, and the real supervisor the way the owner does."""
import json
import os
import re
import socket
import subprocess
import sys
import time

import pytest

from hokma.core import config, control, launchproof
from hokma.core.supervisor import EngineLog, Supervisor, handoff_payload
from supervisor_support import Install, cmdline, find_engine, free_port, health, http_json, wait_until

TOKEN = "T" * 32
KEY = "k" * 64
NONCE = "ab" * 32
MAC = "cd" * 32


# ------------------------------------------------------------------------------------------------ the unit

def test_a_proof_is_redeemed_once_and_only_when_issued():
    authority = launchproof.Authority(key=lambda: b"k" * 32)
    proof = authority.issue()
    nonce, mac = proof.split(".")
    assert launchproof.PROOF_PATTERN.fullmatch(proof) and mac == launchproof.sign(nonce, b"k" * 32)
    assert authority.redeem(nonce) is True
    assert authority.redeem(nonce) is False                      # one use
    assert authority.redeem(NONCE) is False and authority.redeem("not a nonce") is False
    assert authority.issue() != authority.issue()


def test_a_proof_expires():
    now = [100.0]
    authority = launchproof.Authority(key=lambda: None, clock=lambda: now[0])
    nonce = authority.issue().split(".")[0]
    now[0] += launchproof.PROOF_TTL_SECONDS + 1
    assert authority.redeem(nonce) is False


@pytest.mark.parametrize("proof", ["", "x", NONCE, f"{NONCE}.", f"{NONCE}.{MAC[:-1]}", f"{NONCE.upper()}.{MAC}"])
def test_a_malformed_proof_never_reaches_the_supervisor(proof):
    asked = []
    assert launchproof.verify(proof, ask_supervisor=lambda n: asked.append(n) or True, key=lambda: None) is False
    assert asked == []


def test_a_proof_signed_with_another_key_is_refused_even_if_a_fake_supervisor_says_yes():
    forged = f"{NONCE}.{launchproof.sign(NONCE, b'a' * 32)}"
    assert launchproof.verify(forged, ask_supervisor=lambda n: True, key=lambda: b"b" * 32) is False
    assert launchproof.verify(forged, ask_supervisor=lambda n: True, key=lambda: b"a" * 32) is True
    assert launchproof.verify(forged, ask_supervisor=lambda n: False, key=lambda: b"a" * 32) is False


def test_without_a_credential_store_the_control_channel_is_the_whole_check():
    assert launchproof.verify(f"{NONCE}.{MAC}", ask_supervisor=lambda n: True, key=lambda: None) is True
    assert launchproof.verify(f"{NONCE}.{MAC}", ask_supervisor=lambda n: False, key=lambda: None) is False


def test_the_payload_has_a_fifth_line_only_with_a_proof():
    assert handoff_payload(TOKEN, "", "", KEY) == f"{TOKEN}\n\n\n{KEY}\n".encode()
    assert handoff_payload(TOKEN, "", "", KEY, f"{NONCE}.{MAC}") == f"{TOKEN}\n\n\n{KEY}\n{NONCE}.{MAC}\n".encode()


class FakePopen:
    last = None

    def __init__(self, argv, **kwargs):
        import io
        self.stdin, self.stderr, self.kwargs = io.BytesIO(), io.BytesIO(), kwargs
        self.stdin.close = lambda: None
        self.stderr.close = lambda: None
        FakePopen.last = self

    def poll(self):
        return 0

    def wait(self):
        return 0


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX socket")
def test_the_proof_the_supervisor_writes_works_once_on_the_real_control_channel(tmp_path, monkeypatch):
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path))
    authority = launchproof.Authority(key=lambda: None)
    sup = Supervisor(port=1234, token=TOKEN, argv=lambda: ["engine"], log=EngineLog(tmp_path / "e.log", (TOKEN,)),
                     popen=FakePopen, authority=authority, sleep=lambda s: None)
    server = control.ControlServer(sup.status, sup.request_shutdown, None, authority.redeem)
    server.start()
    try:
        assert sup.run() == 0
        lines = FakePopen.last.stdin.getvalue().decode().split("\n")
        proof = lines[4]
        assert launchproof.PROOF_PATTERN.fullmatch(proof) and lines[0] == TOKEN
        assert launchproof.verify(proof, key=lambda: None) is True          # the engine's side, over the real socket
        assert launchproof.verify(proof, key=lambda: None) is False         # the same proof again: refused
        assert control.request("launch " + NONCE) == {"ok": False}
        assert control.request("status")["ok"] is True
    finally:
        server.close()


def test_the_proof_is_not_in_the_arguments_or_the_environment_of_the_launch(tmp_path):
    authority = launchproof.Authority(key=lambda: None)
    sup = Supervisor(port=1234, token=TOKEN, argv=lambda: ["engine"], log=EngineLog(tmp_path / "e.log", (TOKEN,)),
                     popen=FakePopen, authority=authority, sleep=lambda s: None)
    assert sup.run() == 0
    proof = FakePopen.last.stdin.getvalue().decode().split("\n")[4]
    assert proof not in json.dumps(FakePopen.last.kwargs, default=str) and "env" not in FakePopen.last.kwargs


# ------------------------------------------------------------------------- real processes, like an agent

pytestmark_posix = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process model")


def agent_launch(install: Install, port: int, stdin_text: str) -> subprocess.CompletedProcess:
    """What an agent can type: 'printf ... | hokma engine --port N' (here the slot's own entry point)."""
    return subprocess.run([str(install.exe_a), "engine", "--port", str(port)], input=stdin_text, text=True,
                          capture_output=True, timeout=90, env=install.env())


def lines(token=TOKEN, proof=None) -> str:
    return "\n".join([token, "", "", "", *([proof] if proof is not None else [])]) + "\n"


def serves(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


@pytestmark_posix
@pytest.mark.parametrize("stdin_text", [
    lines(),                                           # the old way: only a token
    lines(proof=f"{NONCE}.{MAC}"),                     # a made-up proof
    lines(proof="no proof"),
    "",
])
def test_an_engine_started_by_an_agent_does_not_serve(tmp_path, stdin_text):
    install = Install(tmp_path)
    port = free_port()
    done = agent_launch(install, port, stdin_text)
    assert done.returncode != 0 and not serves(port)
    assert "launch proof" in done.stderr or "no valid session token" in done.stderr, done.stderr[-300:]


@pytestmark_posix
def test_a_made_up_proof_fails_even_with_a_live_supervisor_and_the_engine_never_marks(tmp_path):
    install = Install(tmp_path)
    port, other = free_port(), free_port()
    sup = subprocess.Popen([str(install.exe_a), "start", "--port", str(port), "--no-browser"], env=install.env(),
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, start_new_session=True)
    try:
        line = sup.stdout.readline().strip()
        token = re.search(r"#t=([^&]+)&", line).group(1)
        assert wait_until(lambda: health(port), 40), "the legitimate engine never answered /health"
        assert http_json(f"http://127.0.0.1:{port}/api/setup", token)               # it serves its routes: it is marked
        done = agent_launch(install, other, lines(token=token, proof=f"{NONCE}.{MAC}"))
        assert done.returncode != 0 and not serves(other) and "launch proof" in done.stderr
        engine = find_engine(sup.pid)
        assert engine is not None
        environ = open(f"/proc/{engine}/environ", "rb").read().decode("latin-1")
        assert not re.search(r"[0-9a-f]{64}\.[0-9a-f]{64}", environ + " ".join(cmdline(engine)))
    finally:
        try:
            os.killpg(sup.pid, 9)
        except ProcessLookupError:
            pass
        sup.wait()



def test_the_engine_consumes_the_proof_so_nothing_that_inherits_standard_input_can_read_it(monkeypatch):
    """The proof travels on the engine's standard input and is read once; what stays on the pipe is only what the
    supervisor writes later (the stop line), so a child that inherits the pipe finds no proof there."""
    from hokma.core import engine_main

    read_end, write_end = os.pipe()
    os.write(write_end, handoff_payload(TOKEN, "", "", KEY, f"{NONCE}.{MAC}") + b"stop\n")
    os.close(write_end)
    with os.fdopen(read_end, "r") as stream:
        for _ in range(4):
            stream.readline()
        assert engine_main.read_launch_proof(stream) == f"{NONCE}.{MAC}"
        assert stream.read() == "stop\n"
