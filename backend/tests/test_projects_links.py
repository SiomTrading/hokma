"""Add by link (D6): safe download. No network: a fake resolver and a fake connection stand in for both."""
import os

import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)
from engine_support import png_bytes

from hokma.i18n import t
from hokma.projects import fetch, service
from hokma.projects.errors import ProjectError

LINKS = t("projects.links.file")
PUBLIC = "93.184.216.34"


class FakeResponse:
    def __init__(self, status=200, headers=None, body=b""):
        self.status, self._h, self._body, self._pos = status, {k.lower(): v for k, v in (headers or {}).items()}, body, 0
        self.reads = 0

    def getheader(self, name, default=None):
        return self._h.get(name.lower(), default)

    def read(self, n):
        self.reads += 1
        chunk = self._body[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk


class FakeConn:
    def __init__(self, routes, log, host, address):
        self.routes, self.log, self.host, self.address = routes, log, host, address

    def request(self, method, target, headers=None):
        self.log.append((self.host, self.address, target))
        self.response = self.routes[(self.host, target)]

    def getresponse(self):
        return self.response

    def close(self):
        pass


def world(routes, dns=None):
    log = []
    table = {"files.example.com": [PUBLIC], "other.example.com": [PUBLIC], **(dns or {})}
    resolver = lambda host, port: table[host]
    connector = lambda host, address, port: FakeConn(routes, log, host, address)
    return resolver, connector, log


def get(url, routes, dns=None, **kw):
    resolver, connector, log = world(routes, dns)
    return fetch.fetch(url, resolver=resolver, connector=connector, **kw), log


def refused(url, routes, dns=None, **kw):
    with pytest.raises(ProjectError) as e:
        get(url, routes, dns, **kw)
    return e.value.code


# ------------------------------------------------------------------------------------------- URL rules

@pytest.mark.parametrize("url", ["http://files.example.com/a.pdf", "ftp://files.example.com/a", "file:///etc/passwd",
                                 "https://user:pw@files.example.com/a.pdf", "https://files.example.com:8443/a.pdf",
                                 "https:///a", "javascript:alert(1)", "https://files.example.com/a b", ""])
def test_urls_that_are_not_plain_https_are_refused(url):
    with pytest.raises(ProjectError):
        fetch.check_url(url)


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.5", "192.168.1.1", "172.16.0.9", "169.254.169.254", "0.0.0.0",
                                     "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1", "::ffff:10.0.0.1", "224.0.0.1",
                                     "100.64.0.1", "not-an-ip"])
def test_non_public_addresses_are_not_public(address):
    assert fetch.is_public(address) is False


def test_public_addresses_are_public():
    assert fetch.is_public(PUBLIC) and fetch.is_public("2606:4700:4700::1111")


def test_host_resolving_to_a_private_address_is_refused_before_connecting():
    routes = {}
    resolver, connector, log = world(routes, {"evil.example.com": ["10.0.0.7"], "mixed.example.com": [PUBLIC, "127.0.0.1"]})
    for host in ("evil.example.com", "mixed.example.com"):
        with pytest.raises(ProjectError) as e:
            fetch.fetch(f"https://{host}/a.pdf", resolver=resolver, connector=connector)
        assert e.value.code == "url_private"
    assert log == []


def test_connection_goes_to_the_checked_address():
    routes = {("files.example.com", "/a.pdf"): FakeResponse(200, {"Content-Type": "application/pdf"}, b"%PDF-1.4 x")}
    got, log = get("https://files.example.com/a.pdf", routes)
    assert log == [("files.example.com", PUBLIC, "/a.pdf")] and got.name == "a.pdf" and got.mime == "application/pdf"


# --------------------------------------------------------------------------------------------- redirects

def test_redirect_to_a_private_address_is_refused():
    routes = {("files.example.com", "/a"): FakeResponse(302, {"Location": "https://internal.example.com/x"})}
    assert refused("https://files.example.com/a", routes, {"internal.example.com": ["192.168.0.10"]}) == "url_private"


@pytest.mark.parametrize("location", ["http://files.example.com/x", "https://127.0.0.1/x", "https://[::1]/x",
                                      "https://files.example.com:22/x", "file:///etc/passwd"])
def test_redirect_to_a_bad_target_is_refused(location):
    routes = {("files.example.com", "/a"): FakeResponse(301, {"Location": location})}
    with pytest.raises(ProjectError):
        get("https://files.example.com/a", routes, {"127.0.0.1": ["127.0.0.1"], "::1": ["::1"]})


def test_redirect_to_a_public_host_is_followed_and_loops_are_cut():
    routes = {("files.example.com", "/a"): FakeResponse(302, {"Location": "https://other.example.com/b.txt"}),
              ("other.example.com", "/b.txt"): FakeResponse(200, {"Content-Type": "text/plain"}, b"hola")}
    got, log = get("https://files.example.com/a", routes)
    assert got.data == b"hola" and got.url == "https://other.example.com/b.txt" and len(log) == 2
    loop = {("files.example.com", "/a"): FakeResponse(302, {"Location": "/a"})}
    assert refused("https://files.example.com/a", loop) == "too_many_redirects"


# ------------------------------------------------------------------------------------------ size / type

def test_declared_size_over_the_limit_is_refused_without_reading():
    big = FakeResponse(200, {"Content-Type": "application/pdf", "Content-Length": str(26 * 1024 * 1024)}, b"%PDF")
    assert refused("https://files.example.com/a.pdf", {("files.example.com", "/a.pdf"): big}) == "too_large"
    assert big.reads == 0


def test_streamed_size_over_the_limit_is_cut():
    body = FakeResponse(200, {"Content-Type": "application/pdf"}, b"%PDF" + b"0" * 5000)
    assert refused("https://files.example.com/a.pdf", {("files.example.com", "/a.pdf"): body}, max_bytes=1000) == "too_large"


@pytest.mark.parametrize("ctype,name,body", [
    ("application/x-msdownload", "setup.exe", b"MZ\x90\x00"),
    ("application/octet-stream", "tool.exe", b"MZ\x90\x00"),
    ("application/octet-stream", "data.pdf", b"\x7fELF...."),
    ("application/pdf", "disfrazado.pdf", b"#!/bin/sh\nrm -rf"),
    ("application/zip", "a.zip", b"PK\x03\x04"),
    ("image/png", "falsa.png", b"<html>no soy imagen</html>"),
    ("application/x-sh", "run.sh", b"echo hi"),
    ("application/octet-stream", "sin_ext", b"abc"),
])
def test_types_that_are_not_allowed_are_refused(ctype, name, body):
    routes = {("files.example.com", f"/{name}"): FakeResponse(200, {"Content-Type": ctype}, body)}
    assert refused(f"https://files.example.com/{name}", routes) == "type_blocked"


def test_octet_stream_with_a_good_extension_is_accepted_and_the_page_is_not_downloaded():
    ok = {("files.example.com", "/r.csv"): FakeResponse(200, {"Content-Type": "application/octet-stream"}, b"a,b\n1,2")}
    assert get("https://files.example.com/r.csv", ok)[0].name == "r.csv"
    page = {("files.example.com", "/"): FakeResponse(200, {"Content-Type": "text/html; charset=utf-8"}, b"<html>")}
    got, _ = get("https://files.example.com/", page)
    assert got.data is None


def test_other_statuses_and_empty_bodies():
    gone = {("files.example.com", "/a"): FakeResponse(404, {})}
    assert refused("https://files.example.com/a", gone) == "http_status"
    empty = {("files.example.com", "/a.txt"): FakeResponse(200, {"Content-Type": "text/plain"}, b"")}
    assert refused("https://files.example.com/a.txt", empty) == "empty_download"


def test_name_comes_from_content_disposition_and_is_sanitized():
    routes = {("files.example.com", "/dl"): FakeResponse(200, {"Content-Type": "application/pdf",
              "Content-Disposition": 'attachment; filename="../../informe final.pdf"'}, b"%PDF-1.4")}
    assert get("https://files.example.com/dl", routes)[0].name == "informe final.pdf"


# ---------------------------------------------------------------------------------- end to end, service

def project(db):
    with db.write() as u:
        u.profile.save(owner_name="Ana", fund_name="F")
    return service.create_project("Links", "research", db=db)


def test_link_is_saved_downloaded_committed_and_recorded(db):
    p = project(db)
    routes = {("files.example.com", "/g.png"): FakeResponse(200, {"Content-Type": "image/png"}, png_bytes())}
    resolver, connector, _ = world(routes)
    out = service.add_link(p["id"], "https://files.example.com/g.png", "Grafico NQ", db=db, resolver=resolver, connector=connector)
    assert out["downloaded"] and out["file"] == "imagenes/g.png" and out["git"]["ok"]
    links = open(os.path.join(p["path"], *LINKS.split("/")), encoding="utf-8").read()
    assert "[Grafico NQ](https://files.example.com/g.png)" in links and links.startswith("# Links")
    tree = service.project_tree(p["id"], db)
    assert any(e["path"] == "imagenes/g.png" for e in tree["entries"]) and tree["commits"][0].startswith("Add link Grafico NQ")
    with db.read() as u:
        assert [a.kind for a in u.attachments.list(project_id=p["id"])] == ["image"]


def test_page_link_is_only_saved_and_private_link_saves_nothing(db):
    p = project(db)
    page = {("files.example.com", "/"): FakeResponse(200, {"Content-Type": "text/html"}, b"<html>")}
    resolver, connector, log = world(page)
    out = service.add_link(p["id"], "https://files.example.com/", "Sitio", db=db, resolver=resolver, connector=connector)
    assert out["downloaded"] is False and out["file"] is None
    resolver, connector, log = world({}, {"files.example.com": ["10.1.1.1"]})
    with pytest.raises(ProjectError) as e:
        service.add_link(p["id"], "https://files.example.com/x.pdf", "Interno", db=db, resolver=resolver, connector=connector)
    assert e.value.code == "url_private" and log == []
    links = open(os.path.join(p["path"], *LINKS.split("/")), encoding="utf-8").read()
    assert "Interno" not in links and "Sitio" in links


def test_title_cannot_break_the_markdown(db):
    p = project(db)
    out = service.add_link(p["id"], "https://files.example.com/a(1).pdf", "x](https://evil)\n- injected", download=False, db=db)
    links = open(os.path.join(p["path"], *LINKS.split("/")), encoding="utf-8").read().splitlines()
    assert out["downloaded"] is False and len(links) == 3 and links[-1].count("](") == 1 and "%29" in links[-1]
