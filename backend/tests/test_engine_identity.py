"""The panel address is predictable, so `hokma start` proves who listens on the port before it opens the browser
(task 3078). The engine answers a fresh question with an HMAC only the holder of `<data>/identity.key` can make."""
import http.server
import json
import threading

import pytest
from fastapi.testclient import TestClient

from hokma.core import config, identity
from hokma.core.app import create_app

PORT = 8765
NONCE = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data"


def test_the_key_is_created_once_private_and_never_changes(data):
    first = identity.key()
    assert len(first) == 32 and identity.key() == first
    assert (data / "identity.key").read_text().strip() == first.hex()
    import stat
    import sys
    if sys.platform != "win32":
        assert stat.S_IMODE((data / "identity.key").stat().st_mode) == 0o600


def test_a_damaged_key_file_is_replaced(data):
    data.mkdir()
    (data / "identity.key").write_text("not a key\n")
    key = identity.key()
    assert len(key) == 32 and (data / "identity.key").read_text().strip() == key.hex()


def test_the_proof_depends_on_the_nonce_the_port_and_the_key(data):
    base = identity.proof(NONCE, PORT)
    assert base == identity.proof(NONCE, PORT)
    assert base != identity.proof(NONCE[::-1], PORT) and base != identity.proof(NONCE, PORT + 1)
    (data / "identity.key").write_text("00" * 32 + "\n")
    assert identity.proof(NONCE, PORT) != base


def test_the_engine_answers_the_proof_only_on_the_numeric_loopback_with_a_good_nonce(data):
    client = TestClient(create_app(PORT), base_url=f"http://127.0.0.1:{PORT}")
    ok = client.get(identity.ROUTE, params={"nonce": NONCE})
    assert ok.status_code == 200 and ok.json() == {"proof": identity.proof(NONCE, PORT)}
    for params in ({}, {"nonce": "short"}, {"nonce": NONCE.upper()}, {"nonce": NONCE + "0"}):
        assert client.get(identity.ROUTE, params=params).status_code == 404
    other = TestClient(create_app(PORT), base_url=f"http://localhost:{PORT}")
    assert other.get(identity.ROUTE, params={"nonce": NONCE}).status_code == 404
    named = TestClient(create_app(PORT), base_url=f"http://{config.browser_host()}:{PORT}")
    assert named.get(identity.ROUTE, params={"nonce": NONCE}).status_code in (403, 404)


def _serve(handler_body: bytes | None, status: int = 200):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status)
            self.end_headers()
            if handler_body is not None:
                self.wfile.write(handler_body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.mark.parametrize("body,status", [(None, 404), (b"<html>hi</html>", 200), (b'{"proof": "zz"}', 200),
                                         (json.dumps({"proof": "0" * 64}).encode(), 200)])
def test_another_program_on_the_port_is_refused_at_once(data, body, status):
    server = _serve(body, status)
    try:
        assert identity.listener_is_this_engine(server.server_address[1], wait=0.0) is False
    finally:
        server.shutdown()


def test_an_impostor_that_relays_the_question_to_the_real_engine_on_another_port_is_refused(data):
    """The proof has the port inside, so the real engine's answer for its own port does not fit the impostor's."""
    real = identity.proof

    class Relay(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            nonce = self.path.split("nonce=")[1]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"proof": real(nonce, 9)}).encode())     # the real engine on port 9

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Relay)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert identity.listener_is_this_engine(server.server_address[1], wait=0.0) is False
    finally:
        server.shutdown()


def test_nothing_listening_is_not_the_engine(data):
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    assert identity.listener_is_this_engine(free, wait=0.0) is False


def _ipv6_available() -> bool:
    import socket
    try:
        with socket.socket(socket.AF_INET6) as probe:
            probe.bind(("::1", 0))
        return True
    except OSError:
        return False


class _Real(http.server.BaseHTTPRequestHandler):
    """Stands for the real engine on 127.0.0.1: it answers the proof the way the engine does."""

    def do_GET(self):
        nonce = self.path.split("nonce=")[1]
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps({"proof": identity.proof(nonce, self.server.server_address[1])}).encode())

    def log_message(self, *args):
        pass


class _Impostor6(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"hello")

    def log_message(self, *args):
        pass


class _Server6(http.server.ThreadingHTTPServer):
    address_family = __import__("socket").AF_INET6


@pytest.mark.skipif(not _ipv6_available(), reason="no IPv6 loopback on this computer")
def test_an_impostor_on_the_ipv6_loopback_is_refused_even_when_127_0_0_1_is_the_real_engine(data):
    real = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Real)
    port = real.server_address[1]
    threading.Thread(target=real.serve_forever, daemon=True).start()
    try:
        assert identity.listener_is_this_engine(port, wait=0.0) is True            # alone, the real engine passes
        try:
            fake = _Server6(("::1", port), _Impostor6)
        except OSError:
            pytest.skip("the same port is not free on ::1")
        threading.Thread(target=fake.serve_forever, daemon=True).start()
        try:
            assert identity.listener_is_this_engine(port, wait=0.0) is False
        finally:
            fake.shutdown()
            fake.server_close()
    finally:
        real.shutdown()
        real.server_close()


@pytest.mark.skipif(not _ipv6_available(), reason="no IPv6 loopback on this computer")
def test_a_port_held_on_the_ipv6_loopback_is_a_busy_port_for_the_supervisor(data):
    import socket

    from hokma.core.supervisor import port_in_use, port_in_use_on_ipv6

    with socket.socket(socket.AF_INET6) as held:
        held.bind(("::1", 0))
        held.listen(1)
        port = held.getsockname()[1]
        assert port_in_use_on_ipv6(port) is True and port_in_use(port) is True
    assert port_in_use_on_ipv6(port) is False


def test_the_ipv6_probe_waits_longer_than_a_slow_refusal_on_windows():
    assert identity.IPV6_TIMEOUT_SECONDS >= 5.0 > identity.REQUEST_TIMEOUT_SECONDS
