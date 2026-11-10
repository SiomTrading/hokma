"""Task 3140: the dashboard page is never reused from the browser cache after an update."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hokma.core.dashboard_static import IMMUTABLE, NO_STORE, REVALIDATE, DashboardFiles, cache_control_for


def _client(tmp_path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<!doctype html><script src='/assets/index-abc123.js'></script>")
    (tmp_path / "assets" / "index-abc123.js").write_text("console.log(1)")
    (tmp_path / "licenses").mkdir()
    (tmp_path / "licenses" / "LICENSE").write_text("MIT")
    app = FastAPI()
    app.mount("/", DashboardFiles(directory=tmp_path, html=True), name="dashboard")
    return TestClient(app)


def test_index_is_no_store_without_validators(tmp_path):
    client = _client(tmp_path)
    for url in ("/", "/index.html"):
        res = client.get(url)
        assert res.status_code == 200
        assert res.headers["cache-control"] == NO_STORE
        assert "etag" not in res.headers and "last-modified" not in res.headers


def test_hashed_assets_are_immutable(tmp_path):
    res = _client(tmp_path).get("/assets/index-abc123.js")
    assert res.status_code == 200 and res.headers["cache-control"] == IMMUTABLE


def test_other_files_revalidate(tmp_path):
    res = _client(tmp_path).get("/licenses/LICENSE")
    assert res.status_code == 200 and res.headers["cache-control"] == REVALIDATE


def test_policy():
    assert cache_control_for("index.html", "text/html; charset=utf-8") == NO_STORE
    assert cache_control_for("assets/a-1.css", "text/css") == IMMUTABLE
    assert cache_control_for("favicon.svg", "image/svg+xml") == REVALIDATE
