"""SPEC-010 P9: model download against a local fake server (CI never downloads 135 MB)."""
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from hokma.core.app import create_app
from hokma.library import model_download as md
from hokma.rpc.ws import SESSION_TOKEN

BLOBS = {"model.onnx": b"m" * 300_000 + b"end", "tokenizer.json": b'{"t":1}' * 1000}
HEADERS = {"X-Hokma-Token": SESSION_TOKEN}


class Fake:
    """Serves BLOBS; `fail` requests answer 500, `cut` serves only the first bytes and drops, `ranges` counts Range uses."""

    def __init__(self):
        self.fail = 0
        self.cut = 0
        self.ranges = []
        self.hits = 0
        self.ignore_range = False
        self.corrupt = False
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                fake.hits += 1
                if fake.fail > 0:
                    fake.fail -= 1
                    self.send_response(500)
                    self.end_headers()
                    return
                data = BLOBS[self.path.lstrip("/")]
                if fake.corrupt:
                    data = b"x" * len(data)
                start = 0
                rng = self.headers.get("Range")
                if rng and not fake.ignore_range:
                    start = int(rng.split("=")[1].split("-")[0])
                    fake.ranges.append(start)
                    self.send_response(206)
                else:
                    self.send_response(200)
                body = data[start:]
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if fake.cut > 0:
                    fake.cut -= 1
                    self.wfile.write(body[:1000])
                    self.wfile.flush()
                    self.connection.close()
                    return
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def manifest(self):
        return {"id": "fake", "files": [
            {"name": n, "url": f"{self.url}/{n}", "size": len(b), "sha256": hashlib.sha256(b).hexdigest()}
            for n, b in BLOBS.items()]}


@pytest.fixture
def fake():
    f = Fake()
    yield f
    f.server.shutdown()


def make(fake, tmp_path, waits=(0.05,)):
    return md.ModelDownloader(fake.manifest(), tmp_path / "m", waits=waits)


def wait_state(d, state, timeout=10):
    import time
    end = time.time() + timeout
    while time.time() < end:
        if d.status()["state"] == state:
            return
        time.sleep(0.02)
    raise AssertionError(f"state {d.status()} never reached {state}")


def test_packaged_manifest_is_complete_and_https():
    m = md.load_manifest()
    assert m["license"] == "MIT" and m["dimensions"] == 384
    assert [f["name"] for f in m["files"]] == ["model_quantized.onnx", "tokenizer.json"]
    for f in m["files"]:
        assert f["url"].startswith("https://") and len(f["sha256"]) == 64 and f["size"] > 1_000_000
        assert m["revision"] in f["url"]


def test_manifest_rejects_plain_http(tmp_path):
    p = tmp_path / "m.json"
    p.write_text(json.dumps({"embedding": {"files": [{"name": "a", "url": "http://x/a"}]}}))
    with pytest.raises(ValueError):
        md.load_manifest(p)


def test_reaches_ready_without_intervention(fake, tmp_path):
    d = make(fake, tmp_path)
    assert d.status()["state"] == "downloading"
    d.start()
    wait_state(d, "ready")
    s = d.status()
    assert s["percent"] == 100 and s["bytes"] == s["total"]
    for n, b in BLOBS.items():
        assert d.path_of(n).read_bytes() == b
    assert not list((tmp_path / "m").glob("*.part"))


def test_valid_file_is_not_downloaded_again(fake, tmp_path):
    d = make(fake, tmp_path)
    d.start()
    wait_state(d, "ready")
    hits = fake.hits
    d2 = make(fake, tmp_path)
    d2.start()
    wait_state(d2, "ready")
    assert fake.hits == hits


def test_resumes_a_cut_download(fake, tmp_path):
    fake.cut = 1
    d = make(fake, tmp_path)
    d.start()
    wait_state(d, "ready")
    assert fake.ranges and fake.ranges[0] > 0  # second attempt asked for the rest
    assert d.path_of("model.onnx").read_bytes() == BLOBS["model.onnx"]


def test_server_ignoring_range_restarts_cleanly(fake, tmp_path):
    fake.cut = 1
    fake.ignore_range = True
    d = make(fake, tmp_path)
    d.start()
    wait_state(d, "ready")
    assert d.path_of("model.onnx").read_bytes() == BLOBS["model.onnx"]


def test_three_failures_then_failed_and_retry_downloads(fake, tmp_path):
    fake.fail = 10_000
    d = make(fake, tmp_path, waits=(0.01, 0.01, 30))  # third wait is long: only retry() can end it
    d.start()
    wait_state(d, "failed")
    assert d.retry() is True
    fake.fail = 0
    wait_state(d, "ready")


def test_retry_only_in_failed(fake, tmp_path):
    d = make(fake, tmp_path)
    assert d.retry() is False
    d.start()
    wait_state(d, "ready")
    assert d.retry() is False


def test_automatic_retries_continue_while_failed(fake, tmp_path):
    fake.fail = 10_000
    d = make(fake, tmp_path, waits=(0.01,))
    d.start()
    wait_state(d, "failed")
    fake.fail = 0
    wait_state(d, "ready")  # no retry() call: the loop recovered on its own


def test_wrong_sha256_is_a_failure_and_no_final_file(fake, tmp_path):
    fake.corrupt = True
    # After the third failure the loop sleeps 30 s: no attempt is in flight when the files are checked. With a short wait
    # a new attempt could be writing its .part at that moment (it failed on Windows runners, bug 1097 follow-up).
    d = make(fake, tmp_path, waits=(0.01, 0.01, 30))
    d.start()
    wait_state(d, "failed")
    d.stop()
    d.join(5)
    assert not d.path_of("model.onnx").exists() and not d.path_of("model.onnx.part").exists()


def test_api_status_and_retry(fake, tmp_path, monkeypatch):
    fake.fail = 10_000
    d = make(fake, tmp_path, waits=(0.01, 0.01, 30))
    monkeypatch.setattr(md, "_DOWNLOADER", d)
    client = TestClient(create_app())
    assert client.get("/api/library/model").status_code == 401
    body = client.get("/api/library/model", headers=HEADERS).json()
    assert set(body) == {"state", "percent", "bytes", "total"} and body["state"] == "downloading"
    r = client.post("/api/library/model/retry", headers=HEADERS)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "model_not_failed"
    d.start()
    wait_state(d, "failed")
    fake.fail = 0
    r = client.post("/api/library/model/retry", headers=HEADERS)
    assert r.status_code == 200 and r.json()["state"] in ("downloading", "ready")
    wait_state(d, "ready")
    assert client.get("/api/library/model", headers=HEADERS).json()["state"] == "ready"
    d.stop()


def test_there_is_no_start_endpoint():
    client = TestClient(create_app())
    for method in ("post", "put"):
        assert getattr(client, method)("/api/library/model", headers=HEADERS).status_code in (404, 405)
    assert client.post("/api/library/model/start", headers=HEADERS).status_code in (404, 405)
