"""Add from a link (SPEC-009 P6): discover, download with limits, and the private-host guard.

A real HTTP server runs on 127.0.0.1 inside the fixture. Nothing goes to the internet. The guard is relaxed only for
127.0.0.1 in the tests that need the server; the guard tests run with the real rule.
"""
import http.server
import threading
import time

import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)
from docs_support import make_pdf
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hokma.docs import web
from hokma.docs.api import router as docs_router
from hokma.projects.api import router as projects_router
from hokma.rpc.ws import SESSION_TOKEN

H = {"X-Hokma-Token": SESSION_TOKEN}
PDF_A = make_pdf(["alpha findings about momentum"])
PDF_B = make_pdf(["beta findings about carry"])
PAGE = """<html><head><title>Papers list</title></head><body>
<a href="/files/a.pdf">Alpha paper</a>
<a href="/files/a.pdf#page=2">Alpha again</a>
<a href="b.pdf?download=1"> Beta paper </a>
<a href="/pdf/2401.0001">pdf</a>
<a href="/dl?id=7">[PDF]</a>
<a href="/about">About us</a>
<a href="mailto:x@y.z">mail pdf.pdf</a>
<a href="javascript:void(0)">PDF</a>
<a href="ftp://host/old.pdf">ftp copy</a>
</body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    seen: list[dict] = []

    def log_message(self, *a):
        pass

    def send(self, status, body=b"", ctype="application/octet-stream", extra=None, declared=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body) if declared is None else declared))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        Handler.seen.append({"path": self.path, "headers": dict(self.headers)})
        path = self.path.split("?")[0]
        if path == "/list":
            return self.send(200, PAGE.encode(), "text/html; charset=utf-8")
        if path in ("/files/a.pdf", "/pdf/2401.0001"):
            return self.send(200, PDF_A, "application/pdf")
        if path in ("/files/b.pdf", "/b.pdf"):
            return self.send(200, PDF_B, "application/pdf")
        if path == "/dl":
            return self.send(200, PDF_B + b"\n%x", "application/octet-stream")
        if path == "/mislabeled":
            return self.send(200, PDF_A, "text/plain")
        if path == "/login.pdf":
            return self.send(200, b"<html><body>Please sign in</body></html>", "text/html")
        if path == "/captcha":
            return self.send(200, b"<!doctype html><title>x</title>", "text/html")
        if path in ("/s401", "/s403", "/s429"):
            return self.send(int(path[2:]), b"no")
        if path == "/s404":
            return self.send(404, b"no")
        if path == "/huge.pdf":
            return self.send(200, b"", "application/pdf", declared=60 * 1024 * 1024)
        if path == "/stream.pdf":
            return self.send(200, b"%PDF-1.4\n" + b"x" * 4000, "application/pdf")
        if path == "/notdoc.bin":
            return self.send(200, b"\x01\x02\x03 nothing here", "application/octet-stream")
        if path == "/data.csv":
            return self.send(200, b"a,b\n1,2\n", "text/csv")
        if path == "/page.html":
            return self.send(200, b"<html><head><title>T</title></head><body><h1>Hello page</h1><p>text</p></body></html>",
                             "text/html")
        if path == "/empty":
            return self.send(200, b"", "application/pdf")
        if path.startswith("/r/"):
            hop = int(path[3:])
            if hop >= 7:
                return self.send(200, PDF_A, "application/pdf")
            return self.send(302, b"", extra={"Location": f"/r/{hop + 1}"})
        if path == "/r2":
            return self.send(302, b"", extra={"Location": "/mid"})
        if path == "/mid":
            return self.send(302, b"", extra={"Location": "/files/a.pdf"})
        if path == "/to-private":
            return self.send(302, b"", extra={"Location": "http://10.0.0.5/secret.pdf"})
        if path == "/to-loopback-name":
            return self.send(302, b"", extra={"Location": "http://[::1]:1/x.pdf"})
        if path == "/to-metadata":
            return self.send(302, b"", extra={"Location": "http://169.254.169.254/latest/meta-data/"})
        if path == "/to-ftp":
            return self.send(302, b"", extra={"Location": "ftp://example.com/a.pdf"})
        if path == "/no-location":
            return self.send(302, b"")
        if path == "/trickle.pdf":
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", "100000")
            self.end_headers()
            try:
                for _ in range(40):
                    self.wfile.write(b"%PDF-" if _ == 0 else b"x")
                    self.wfile.flush()
                    time.sleep(0.1)
            except OSError:
                pass
            return
        if path == "/slow":
            time.sleep(1.5)
            return self.send(200, PDF_A, "application/pdf")
        self.send(404, b"missing")


@pytest.fixture
def server():
    Handler.seen = []
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def local_ok(monkeypatch):
    """Let the guard accept 127.0.0.1 only, so the local test server can be reached. Everything else stays real."""
    real = web.is_public
    monkeypatch.setattr(web, "is_public", lambda address: address == "127.0.0.1" or real(address))


@pytest.fixture
def pauses(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(web, "_sleep", slept.append)
    web._LAST_HIT.clear()
    yield slept
    web._LAST_HIT.clear()


@pytest.fixture
def client(db, monkeypatch, pauses):
    monkeypatch.setenv("HOKMA_LANG", "es")
    with db.write() as u:
        u.profile.save(owner_name="Ana", fund_name="Fondo Prueba")
    app = FastAPI()
    app.include_router(projects_router)
    app.include_router(docs_router)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def project(client):
    return client.post("/api/projects", headers=H, json={"name": "Alpha", "kind": "strategy"}).json()["id"]


def discover(client, project_id, link):
    return client.post(f"/api/projects/{project_id}/documents/discover", headers=H, json={"url": link})


def fetch(client, project_id, link, title=None):
    body = {"url": link} if title is None else {"url": link, "title": title}
    return client.post(f"/api/projects/{project_id}/documents/fetch", headers=H, json=body)


def detail(res):
    return res.json()["detail"]


# ------------------------------------------------------------------------------------------------------ discover

def test_discover_lists_the_pdf_links_of_a_page_and_the_page_itself(client, project, server, local_ok):
    res = discover(client, project, f"{server}/list")
    assert res.status_code == 200
    body = res.json()
    assert body["page_is_document"] is False and body["max_select"] == 50
    urls = [i["url"] for i in body["items"]]
    assert urls == [f"{server}/files/a.pdf", f"{server}/b.pdf?download=1", f"{server}/pdf/2401.0001",
                    f"{server}/dl?id=7", f"{server}/list"]
    kinds = [i["kind"] for i in body["items"]]
    assert kinds == ["pdf", "pdf", "pdf", "pdf", "html"]
    assert body["items"][0]["title"] == "Alpha paper" and body["items"][1]["title"] == "Beta paper"
    assert body["items"][2]["title"] == "2401.0001"          # a link whose text is just "pdf" is named after its path
    assert body["items"][4]["title"] == "Papers list"
    assert all(i["host"] == "127.0.0.1" for i in body["items"])


def test_discover_on_a_pdf_lists_that_document_alone(client, project, server, local_ok):
    body = discover(client, project, f"{server}/files/a.pdf").json()
    assert body["page_is_document"] is True
    assert body["items"] == [{"url": f"{server}/files/a.pdf", "title": "a.pdf", "kind": "pdf", "host": "127.0.0.1"}]


def test_discover_recognizes_a_pdf_by_its_bytes_when_the_type_is_wrong(client, project, server, local_ok):
    body = discover(client, project, f"{server}/mislabeled").json()
    assert body["page_is_document"] is True and body["items"][0]["kind"] == "pdf"


def test_discover_caps_the_list_at_200(monkeypatch):
    links = "".join(f'<a href="/p/{n}.pdf">p{n}</a>' for n in range(300))
    title, items = web.parse_page(f"<html><body>{links}</body></html>", "https://example.org/")
    assert len(items) == web.MAX_LISTED - 1 and items[0]["url"] == "https://example.org/p/0.pdf"


def test_discover_reads_at_most_5_mb_of_a_page(monkeypatch, client, project, server, local_ok):
    monkeypatch.setattr(web, "MAX_HTML_BYTES", 10)
    body = discover(client, project, f"{server}/list").json()
    assert [i["kind"] for i in body["items"]] == ["html"]    # the cut page had no complete link left


def test_discover_on_something_that_is_not_a_document(client, project, server, local_ok):
    res = discover(client, project, f"{server}/notdoc.bin")
    assert res.status_code == 422 and detail(res)["reason"] == "not_a_document"


@pytest.mark.parametrize("path,reason", [("/s401", "blocked"), ("/s403", "blocked"), ("/s429", "blocked"),
                                         ("/s404", "network")])
def test_discover_classifies_refusals(client, project, server, local_ok, path, reason):
    res = discover(client, project, f"{server}{path}")
    assert detail(res)["reason"] == reason


# ------------------------------------------------------------------------------------------------------ guard

@pytest.mark.parametrize("link", [
    "http://127.0.0.1:9/a.pdf", "http://localhost/a.pdf", "http://[::1]/a.pdf", "http://10.0.0.5/a.pdf",
    "http://192.168.1.1/a.pdf", "http://172.16.0.9/a.pdf", "http://169.254.169.254/latest/meta-data/",
    "http://0.0.0.0/a.pdf", "http://224.0.0.1/a.pdf", "http://[::ffff:127.0.0.1]/a.pdf", "http://[fe80::1]/a.pdf",
    "https://100.64.0.1/a.pdf", "http://2130706433/a.pdf",
])
def test_private_destinations_are_refused_before_any_connection(client, project, link):
    for call in (discover, fetch):
        res = call(client, project, link)
        assert res.status_code == 400 and detail(res)["code"] == "url_private" and detail(res)["reason"] == "network"


def _no_resolver(host, port):
    raise AssertionError("the system resolver must not be reached for a numeric private host")


@pytest.mark.parametrize("host", ["0x7f000001", "017700000001", "127.1", "2130706433", "0x7f.1", "0x7f.0.0.1",
                                  "0177.0.0.1", "0xa000001", "1.2.3.4.5", "0x", "08.1", "4294967296", "256.1"])
def test_numeric_hosts_are_refused_without_the_system_resolver(host):
    for call in (web.discover, web.download):
        with pytest.raises(web.LinkError) as err:
            call(f"http://{host}/a.pdf", resolver=_no_resolver)
        assert err.value.code == "url_private" and err.value.status == 400


def test_a_numeric_public_host_is_normalized_before_resolving():
    seen = []

    def resolver(host, port):
        seen.append(host)
        return ["10.0.0.1"]

    with pytest.raises(web.LinkError):
        web.discover("http://1572395042/a.pdf", resolver=resolver)   # 93.184.216.34
    assert seen == ["93.184.216.34"]


def test_a_name_that_resolves_to_a_private_address_is_refused(monkeypatch):
    with pytest.raises(web.LinkError) as err:
        web.discover("http://papers.example.org/", resolver=lambda host, port: ["93.184.216.34", "10.1.2.3"])
    assert err.value.code == "url_private"


def test_a_redirect_to_a_private_host_is_refused(client, project, server, local_ok):
    for target in ("/to-private", "/to-loopback-name", "/to-metadata"):
        for call in (discover, fetch):
            res = call(client, project, f"{server}{target}")
            assert res.status_code == 400 and detail(res)["code"] == "url_private", target
    assert not any(s["path"].startswith("/secret") for s in Handler.seen)


def test_a_redirect_to_another_scheme_is_refused(client, project, server, local_ok):
    res = fetch(client, project, f"{server}/to-ftp")
    assert res.status_code == 400 and detail(res)["code"] == "link_scheme"


def test_the_connection_goes_to_the_checked_address(server, local_ok):
    """The resolver answer is what is dialed: the host name never gets a second lookup."""
    dialed = []
    real = web.default_connector

    def connector(scheme, host, address, port):
        dialed.append((host, address))
        return real(scheme, host, address, port)

    port = server.rsplit(":", 1)[1]
    result = web.discover(f"http://papers.example.org:{port}/list", resolver=lambda h, p: ["127.0.0.1"],
                          connector=connector)
    assert dialed == [("papers.example.org", "127.0.0.1")] and result["items"]


@pytest.mark.parametrize("link", ["ftp://example.com/a.pdf", "file:///etc/passwd", "javascript:alert(1)", "//example.com/a",
                                  "example.com/a.pdf"])
def test_only_http_and_https_are_accepted(client, project, link):
    res = discover(client, project, link)
    assert res.status_code == 400 and detail(res)["code"] == "link_scheme"


@pytest.mark.parametrize("link", ["http://user:pass@example.com/a.pdf", "http://exa mple.com/a", "http://[bad/a.pdf",
                                  "http://example.com:99999/a.pdf"])
def test_malformed_and_credentialed_links_are_refused(client, project, link):
    res = discover(client, project, link)
    assert res.status_code == 400 and detail(res)["code"] == "bad_url"


def test_five_redirects_are_followed_and_the_sixth_is_not(client, project, server, local_ok):
    assert fetch(client, project, f"{server}/r2").status_code == 201
    res = discover(client, project, f"{server}/r/0")          # 7 hops needed: more than 5
    assert res.status_code == 502 and detail(res)["code"] == "too_many_redirects"


def test_a_redirect_without_location_is_a_network_error(client, project, server, local_ok):
    assert detail(discover(client, project, f"{server}/no-location"))["reason"] == "network"


def test_the_request_is_generic_and_carries_nothing_of_the_student(client, project, server, local_ok):
    discover(client, project, f"{server}/list")
    headers = {k.lower(): v for k, v in Handler.seen[0]["headers"].items()}
    assert headers["user-agent"].startswith("Hokma/") and "cookie" not in headers and "authorization" not in headers
    assert headers["accept-encoding"] == "identity"


# ------------------------------------------------------------------------------------------------------ fetch

def test_fetch_downloads_indexes_and_records_the_link(client, project, server, local_ok):
    res = fetch(client, project, f"{server}/files/a.pdf", "Alpha paper")
    assert res.status_code == 201
    doc = res.json()
    assert doc["name"] == "Alpha paper.pdf" and doc["format"] == "pdf" and doc["status"] == "ready"
    assert doc["origin"] == "link" and doc["source_url"] == f"{server}/files/a.pdf" and doc["chunk_count"] > 0
    listed = client.get(f"/api/projects/{project}/documents", headers=H).json()["documents"]
    assert [d["id"] for d in listed] == [doc["id"]]


def test_fetch_names_the_file_from_the_link_when_there_is_no_title(client, project, server, local_ok):
    assert fetch(client, project, f"{server}/files/b.pdf").json()["name"] == "b.pdf"
    assert fetch(client, project, f"{server}/pdf/2401.0001").json()["name"] == "2401.0001.pdf"
    assert fetch(client, project, f"{server}/data.csv").json()["name"] == "data.csv"


def test_fetch_follows_redirects_and_records_the_final_address(client, project, server, local_ok):
    doc = fetch(client, project, f"{server}/r2").json()
    assert doc["source_url"] == f"{server}/files/a.pdf"


def test_fetch_of_a_page_that_is_not_a_pdf_link_indexes_it_as_html(client, project, server, local_ok):
    doc = fetch(client, project, f"{server}/page.html", "The page").json()
    assert doc["format"] == "html" and doc["name"] == "The page.html" and doc["status"] == "ready"


def test_a_second_fetch_of_the_same_document_is_a_duplicate(client, project, server, local_ok):
    assert fetch(client, project, f"{server}/files/a.pdf").status_code == 201
    res = fetch(client, project, f"{server}/pdf/2401.0001")   # other link, same bytes
    assert res.status_code == 409 and detail(res)["reason"] == "duplicate" and detail(res)["code"] == "doc_duplicate"


@pytest.mark.parametrize("path", ["/s401", "/s403", "/s429"])
def test_fetch_marks_401_403_and_429_as_blocked(client, project, server, local_ok, path):
    res = fetch(client, project, f"{server}{path}")
    assert res.status_code == 422 and detail(res)["reason"] == "blocked" and detail(res)["code"] == "link_blocked"
    assert "navegador" in detail(res)["message"]


def test_html_where_a_pdf_was_expected_is_blocked(client, project, server, local_ok):
    for path in ("/login.pdf",):
        res = fetch(client, project, f"{server}{path}")
        assert detail(res)["reason"] == "blocked", path
    assert client.get(f"/api/projects/{project}/documents", headers=H).json() == {"documents": []}


def test_a_declared_size_over_the_limit_is_refused_without_reading(client, project, server, local_ok):
    res = fetch(client, project, f"{server}/huge.pdf")
    assert res.status_code == 413 and detail(res)["reason"] == "too_large"


def test_a_download_is_cut_when_it_passes_the_limit(monkeypatch, client, project, server, local_ok):
    monkeypatch.setattr(web, "MAX_BYTES", 1000)
    res = fetch(client, project, f"{server}/stream.pdf")
    assert res.status_code == 413 and detail(res)["reason"] == "too_large"


def test_the_download_limit_is_50_mb():
    assert web.MAX_BYTES == 50 * 1024 * 1024 and web.TIMEOUT_SECONDS == 20 and web.MAX_REDIRECTS == 5
    assert web.MAX_LISTED == 200 and web.MAX_SELECTED == 50 and web.MAX_HTML_BYTES == 5 * 1024 * 1024


def test_a_type_outside_the_supported_formats_is_not_a_document(client, project, server, local_ok):
    res = fetch(client, project, f"{server}/notdoc.bin")
    assert res.status_code == 422 and detail(res)["reason"] == "not_a_document"


def test_an_empty_answer_is_not_a_document(client, project, server, local_ok):
    assert detail(fetch(client, project, f"{server}/empty"))["reason"] == "not_a_document"


def test_a_slow_server_ends_as_timeout(monkeypatch, client, project, server, local_ok):
    monkeypatch.setattr(web, "TIMEOUT_SECONDS", 0.3)
    res = fetch(client, project, f"{server}/slow")
    assert res.status_code == 504 and detail(res)["reason"] == "timeout"


def test_a_server_that_trickles_bytes_cannot_outlast_the_total_cap(monkeypatch, client, project, server, local_ok):
    monkeypatch.setattr(web, "DOWNLOAD_SECONDS", 0.5)
    started = time.monotonic()
    res = fetch(client, project, f"{server}/trickle.pdf")
    assert res.status_code == 504 and detail(res)["reason"] == "timeout" and time.monotonic() - started < 3


def test_a_closed_port_ends_as_network(client, project, local_ok):
    res = fetch(client, project, "http://127.0.0.1:1/a.pdf")
    assert res.status_code == 502 and detail(res)["reason"] == "network"


def test_an_unresolvable_name_ends_as_network(client, project, monkeypatch):
    def fail(host, port):
        raise web.ProjectError("dns_failed", 502, host=host)

    monkeypatch.setattr(web, "system_resolver", fail)
    res = fetch(client, project, "http://nowhere.invalid/a.pdf")
    assert res.status_code == 502 and detail(res)["reason"] == "network" and detail(res)["code"] == "dns_failed"


def test_downloads_from_the_same_host_wait_one_second(client, project, server, local_ok, pauses):
    fetch(client, project, f"{server}/files/a.pdf")
    fetch(client, project, f"{server}/files/b.pdf")
    assert len(pauses) == 1 and 0 < pauses[0] <= 1.0
    web._LAST_HIT.clear()
    pauses.clear()
    fetch(client, project, f"{server}/data.csv")
    assert pauses == []                                       # first download from a host does not wait


def test_discover_does_not_wait(client, project, server, local_ok, pauses):
    discover(client, project, f"{server}/list")
    discover(client, project, f"{server}/list")
    assert pauses == []


def test_the_selection_flow_three_marked_documents_end_ready(client, project, server, local_ok):
    items = discover(client, project, f"{server}/list").json()["items"]
    marked = [i for i in items if i["kind"] == "pdf"][:3]
    results = [fetch(client, project, i["url"], i["title"]) for i in marked]
    assert [r.status_code for r in results] == [201, 201, 409]    # the third link carries the same bytes as the first
    ready = client.get(f"/api/projects/{project}/documents", headers=H).json()["documents"]
    assert [d["status"] for d in ready] == ["ready", "ready"]


# ------------------------------------------------------------------------------------------------------ access

def test_the_endpoints_need_the_token_and_a_known_project(client, project, server, local_ok):
    base = f"/api/projects/{project}/documents"
    assert client.post(f"{base}/discover", json={"url": f"{server}/list"}).status_code == 401
    assert client.post(f"{base}/fetch", json={"url": f"{server}/list"}).status_code == 401
    assert discover(client, 99999, f"{server}/list").status_code == 404
    assert fetch(client, 99999, f"{server}/files/a.pdf").status_code == 404
    assert Handler.seen == []                                  # a missing project is rejected before any request goes out


def test_a_body_without_url_is_rejected(client, project):
    res = client.post(f"/api/projects/{project}/documents/discover", headers=H, json={})
    assert res.status_code == 422
