"""Discover and download documents from a link (SPEC-009 P6, section 4.6).

Rules, all enforced here and not by the caller:
- Only `http` and `https`, no credentials in the URL, at most 5 redirects, 20 seconds per request.
- Every host name is resolved here and every address must be public (no loopback, private, link-local, reserved,
  multicast). The check runs before each connection and on each redirect, and the connection goes to the address that
  was checked, so a name that changes its answer cannot slip through.
- Discover lists a PDF alone, or the PDF-looking links of an HTML page (at most 5 MB read, at most 200 listed).
- Download stops at 50 MB, waits 1 second between downloads from the same host, and classifies refusals
  (401, 403, 429, or HTML where a PDF was expected) as `blocked` so the student gets the "open it in your browser" path.
"""
import http.client
import ipaddress
import re
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Callable
from urllib.parse import unquote, urldefrag, urljoin, urlsplit

from hokma import __version__
from hokma.docs import ingest
from hokma.docs.readers import detect_format
from hokma.engine.attachments import safe_name
from hokma.projects.errors import ProjectError
from hokma.projects.fetch import Resolver, is_public, system_resolver

MAX_REDIRECTS = 5
TIMEOUT_SECONDS = 20
DOWNLOAD_SECONDS = 120
MAX_HTML_BYTES = 5 * 1024 * 1024
MAX_LISTED = 200
MAX_SELECTED = 50
MAX_BYTES = ingest.MAX_DOCUMENT_BYTES
HOST_PAUSE_SECONDS = 1.0
CHUNK = 64 * 1024
MAX_TITLE_CHARS = 120
BLOCKED_STATUSES = (401, 403, 429)
REDIRECTS = (301, 302, 303, 307, 308)
HTML_TYPES = ("text/html", "application/xhtml+xml")

Connector = Callable[[str, str, str, int], http.client.HTTPConnection]

_PACE_LOCK = threading.Lock()
_LAST_HIT: dict[str, float] = {}


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


class LinkError(ProjectError):
    """A ProjectError that also carries the `reason` the interface uses (contract in section 4.5)."""

    def __init__(self, code: str, status: int, reason: str, **params: object) -> None:
        self.reason = reason
        super().__init__(code, status, **params)


# ---------------------------------------------------------------------------------------------------- connection

class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host: str, address: str, port: int) -> None:
        super().__init__(host, port, timeout=TIMEOUT_SECONDS)
        self._address = address

    def connect(self) -> None:
        self.sock = socket.create_connection((self._address, self.port), self.timeout)


class _PinnedHTTPS(http.client.HTTPSConnection):
    """TLS to the checked address, certificate verified against the original host name."""

    def __init__(self, host: str, address: str, port: int) -> None:
        super().__init__(host, port, timeout=TIMEOUT_SECONDS, context=ssl.create_default_context())
        self._address = address

    def connect(self) -> None:
        sock = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def default_connector(scheme: str, host: str, address: str, port: int) -> http.client.HTTPConnection:
    return _PinnedHTTPS(host, address, port) if scheme == "https" else _PinnedHTTP(host, address, port)


_NUMERIC_LABEL = re.compile(r"(0x[0-9a-f]*|[0-9]+)", re.IGNORECASE)


def _numeric_host(host: str) -> str | None:
    """Canonical IPv4 for a host written as numbers (2130706433, 0x7f000001, 017700000001, 127.1, 0x7f.1), else None.

    Done here, with inet_aton rules, so the verdict never depends on what the operating system resolver understands
    (Windows getaddrinfo rejects these forms). A host that looks numeric but cannot be read is refused.
    """
    labels = host.split(".")
    if not all(_NUMERIC_LABEL.fullmatch(label) for label in labels):
        return None
    if len(labels) > 4:
        raise LinkError("url_private", 400, "network")
    values = []
    for label in labels:
        try:
            if label[:2].lower() == "0x":
                values.append(int(label[2:], 16))
            elif len(label) > 1 and label[0] == "0":
                values.append(int(label, 8))
            else:
                values.append(int(label, 10))
        except ValueError as exc:
            raise LinkError("url_private", 400, "network") from exc
    head, last = values[:-1], values[-1]
    if any(v > 255 for v in head) or last >= 256 ** (4 - len(head)):
        raise LinkError("url_private", 400, "network")
    number = last
    for index, v in enumerate(head):
        number += v << (8 * (3 - index))
    return str(ipaddress.IPv4Address(number))


def check_url(url: str) -> tuple[str, str, int, str]:
    """(scheme, host, port, request target) of a link that may be fetched, or LinkError."""
    if not isinstance(url, str) or not url or len(url) > 2000 or any(ord(c) < 33 for c in url):
        raise LinkError("bad_url", 400, "network")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise LinkError("bad_url", 400, "network") from exc
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.hostname:
        raise LinkError("link_scheme", 400, "network")
    if parts.username is not None or parts.password is not None:
        raise LinkError("bad_url", 400, "network")
    target = parts.path or "/"
    if parts.query:
        target += "?" + parts.query
    host = parts.hostname.rstrip(".").lower()
    numeric = _numeric_host(host)
    if numeric is not None:
        if not is_public(numeric):
            raise LinkError("url_private", 400, "network")
        host = numeric
    return scheme, host, port or (443 if scheme == "https" else 80), target


@dataclass
class _Opened:
    response: http.client.HTTPResponse
    conn: http.client.HTTPConnection
    url: str          # final URL after redirects
    host: str


def _open_one(connect: Connector, scheme: str, host: str, port: int, addresses: list[str], target: str):
    last: Exception | None = None
    for address in addresses:
        conn = connect(scheme, host, address, port)
        try:
            conn.request("GET", target, headers={"User-Agent": f"Hokma/{__version__}", "Accept-Encoding": "identity",
                                                 "Accept": "*/*", "Connection": "close"})
            return conn.getresponse(), conn
        except TimeoutError as exc:
            conn.close()
            raise LinkError("timeout", 504, "timeout") from exc
        except (OSError, http.client.HTTPException) as exc:
            last = exc
            conn.close()
    raise LinkError("connect_failed", 502, "network", host=host) from last


def _open(url: str, resolver: Resolver | None, connector: Connector | None) -> _Opened:
    """GET the link, following redirects by hand; every hop goes through the same checks. Bad statuses are raised."""
    resolve = resolver or system_resolver
    connect = connector or default_connector
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        scheme, host, port, target = check_url(current)
        try:
            addresses = resolve(host, port)
        except ProjectError as exc:
            raise LinkError(exc.code, exc.status, "network", **exc.params) from exc
        if not addresses or not all(is_public(a) for a in addresses):
            raise LinkError("url_private", 400, "network")
        response, conn = _open_one(connect, scheme, host, port, addresses, target)
        if response.status in REDIRECTS:
            location = response.getheader("Location")
            conn.close()
            if not location:
                raise LinkError("http_status", 502, "network", code_http=response.status)
            current = urljoin(current, location)
            continue
        if response.status in BLOCKED_STATUSES:
            conn.close()
            raise LinkError("link_blocked", 422, "blocked")
        if response.status != 200:
            conn.close()
            raise LinkError("http_status", 502, "network", code_http=response.status)
        return _Opened(response, conn, current, host)
    raise LinkError("too_many_redirects", 502, "network")


def _read(opened: _Opened, limit: int, *, strict: bool) -> bytes:
    """Read up to `limit` bytes. Past it: raise `too_large` when strict, else stop quietly with what fits."""
    response = opened.response
    deadline = time.monotonic() + DOWNLOAD_SECONDS
    data = bytearray()
    try:
        length = response.getheader("Content-Length")
        if strict and length and length.isdigit() and int(length) > limit:
            raise LinkError("doc_too_large", 413, "too_large")
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LinkError("timeout", 504, "timeout")
            if opened.conn.sock is not None:
                opened.conn.sock.settimeout(min(remaining, TIMEOUT_SECONDS))   # a trickling server cannot outlast the cap
            chunk = response.read1(CHUNK)
            if not chunk:
                break
            data += chunk
            if len(data) > limit:
                if strict:
                    raise LinkError("doc_too_large", 413, "too_large")
                del data[limit:]
                break
    except TimeoutError as exc:
        raise LinkError("timeout", 504, "timeout") from exc
    except (OSError, http.client.HTTPException) as exc:
        raise LinkError("connect_failed", 502, "network", host=opened.host) from exc
    finally:
        opened.conn.close()
    return bytes(data)


# ---------------------------------------------------------------------------------------------------- classification

def _declared(opened: _Opened) -> str:
    return (opened.response.getheader("Content-Type") or "").split(";")[0].strip().lower()


def _looks_pdf(head: bytes) -> bool:
    return head[:5] == b"%PDF-"


def _looks_html(declared: str, head: bytes) -> bool:
    if declared in HTML_TYPES:
        return True
    return head[:512].lstrip().lower().startswith((b"<!doctype html", b"<html", b"<head", b"<body"))


def _path_of(url: str) -> str:
    return unquote(urlsplit(url).path)


def expects_pdf(url: str) -> bool:
    path = _path_of(url).lower()
    return path.endswith(".pdf") or "/pdf/" in path or path.endswith("/pdf")


def _charset(opened: _Opened) -> str:
    match = re.search(r"charset=([\w.-]+)", opened.response.getheader("Content-Type") or "", re.IGNORECASE)
    return match.group(1) if match else "utf-8"


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.links: list[tuple[str, str]] = []
        self._in_title = False
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        elif tag == "a":
            self._close_link()
            self._href = dict(attrs).get("href") or None
            self._text = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag == "a":
            self._close_link()

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        if self._href is not None:
            self._text.append(data)

    def close(self) -> None:
        super().close()
        self._close_link()

    def _close_link(self) -> None:
        if self._href is not None:
            self.links.append((self._href, " ".join("".join(self._text).split())))
        self._href = None
        self._text = []


def _is_pdf_link(href: str, text: str) -> bool:
    path = unquote(urlsplit(href).path).lower()
    label = text.strip().strip("[](){}<>.,:;|").strip().lower()
    return path.endswith(".pdf") or "/pdf/" in path or label == "pdf"


def _title_from(url: str) -> str:
    return _path_of(url).rstrip("/").rsplit("/", 1)[-1] or urlsplit(url).hostname or "document"


def parse_page(html: str, base_url: str) -> tuple[str, list[dict[str, str]]]:
    """(page title, PDF-looking links as absolute, de-duplicated items), at most MAX_LISTED - 1 of them."""
    parser = _Links()
    parser.feed(html)
    parser.close()
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for href, text in parser.links:
        if not _is_pdf_link(href, text):
            continue
        absolute = urldefrag(urljoin(base_url, href.strip()))[0]
        if urlsplit(absolute).scheme.lower() not in ("http", "https") or absolute in seen:
            continue
        seen.add(absolute)
        label = text if text and text.strip().lower() != "pdf" else _title_from(absolute)
        items.append({"url": absolute, "title": label[:MAX_TITLE_CHARS], "kind": "pdf",
                      "host": urlsplit(absolute).hostname or ""})
        if len(items) >= MAX_LISTED - 1:
            break
    return " ".join(parser.title.split()), items


# ---------------------------------------------------------------------------------------------------- discover

def _single_pdf(opened: _Opened) -> dict:
    item = {"url": opened.url, "title": _title_from(opened.url), "kind": "pdf", "host": opened.host}
    return {"items": [item], "page_is_document": True, "max_select": MAX_SELECTED}


def discover(url: str, *, resolver: Resolver | None = None, connector: Connector | None = None) -> dict:
    """{items: [{url, title, kind, host}], page_is_document, max_select}. A PDF lists itself; a page lists its PDF
    links and, last, the page itself as HTML."""
    opened = _open(url, resolver, connector)
    declared = _declared(opened)
    if declared not in HTML_TYPES:
        head = _read(opened, 16, strict=False)
        if declared == "application/pdf" or _looks_pdf(head):
            return _single_pdf(opened)
        raise LinkError("link_not_document", 422, "not_a_document")
    raw = _read(opened, MAX_HTML_BYTES, strict=False)
    if _looks_pdf(raw):
        return _single_pdf(opened)
    try:
        html = raw.decode(_charset(opened), errors="replace")
    except LookupError:
        html = raw.decode("utf-8", errors="replace")
    title, items = parse_page(html, opened.url)
    items.append({"url": opened.url, "title": (title or opened.host)[:MAX_TITLE_CHARS], "kind": "html",
                  "host": opened.host})
    return {"items": items, "page_is_document": False, "max_select": MAX_SELECTED}


# ---------------------------------------------------------------------------------------------------- download

def _pace(host: str) -> None:
    """One second between downloads from the same host."""
    with _PACE_LOCK:
        wait = _LAST_HIT.get(host, 0.0) + HOST_PAUSE_SECONDS - time.monotonic()
        if wait > 0:
            _sleep(wait)
        _LAST_HIT[host] = time.monotonic()


def _disposition_name(value: str | None) -> str:
    if not value or "filename=" not in value:
        return ""
    return unquote(value.split("filename=", 1)[1].split(";")[0].strip().strip('"'))


def _document_name(opened: _Opened, title: str | None, extension: str) -> str:
    base = safe_name(_disposition_name(opened.response.getheader("Content-Disposition"))
                     or _path_of(opened.url).rstrip("/").rsplit("/", 1)[-1])
    stem = " ".join((title or "").split()) or base
    if detect_format(stem) is not None:
        stem = stem.rsplit(".", 1)[0]
    stem = safe_name(stem[:MAX_TITLE_CHARS]) if stem else "document"
    return ("document" if stem == "file" else stem) + extension


def download(url: str, title: str | None = None, *, resolver: Resolver | None = None,
             connector: Connector | None = None) -> tuple[str, bytes, str]:
    """(file name, bytes, final URL) of one document. Raises LinkError with the contract's `reason`."""
    _pace(check_url(url)[1])
    opened = _open(url, resolver, connector)
    declared = _declared(opened)
    data = _read(opened, MAX_BYTES, strict=True)
    if not data:
        raise LinkError("empty_download", 502, "not_a_document")
    pdf = _looks_pdf(data)
    html = not pdf and _looks_html(declared, data)
    if html and (expects_pdf(url) or expects_pdf(opened.url) or declared == "application/pdf"):
        raise LinkError("link_blocked", 422, "blocked")   # captcha or login page where a PDF was expected
    if pdf:
        extension = ".pdf"
    elif html:
        extension = ".html"
    else:
        path_name = _path_of(opened.url).rsplit("/", 1)[-1]
        fmt_name = _disposition_name(opened.response.getheader("Content-Disposition")) or path_name
        if detect_format(fmt_name) is None:
            raise LinkError("link_not_document", 422, "not_a_document")
        extension = "." + fmt_name.rsplit(".", 1)[-1].lower()
    name = _document_name(opened, title, extension)
    if detect_format(name) is None:
        raise LinkError("link_not_document", 422, "not_a_document")
    return name, data, opened.url


_ADD_REASONS = {"doc_duplicate": "duplicate", "doc_unsupported": "not_a_document", "doc_too_large": "too_large"}


def check_project(project_id: int, db=None) -> None:
    ingest._require_project(ingest._db(db), project_id)


def fetch_document(url: str, title: str | None = None, *, folder_id: int | None = None,
                   origin_project_id: int | None = None, resolver: Resolver | None = None,
                   connector: Connector | None = None, db=None) -> dict:
    """Download one link and index it as a Library document (same path as an uploaded file)."""
    name, data, final_url = download(url, title, resolver=resolver, connector=connector)
    try:
        return ingest.add_library_document(name, data, source_url=final_url, folder_id=folder_id,
                                           origin_project_id=origin_project_id, db=db)
    except ProjectError as exc:
        reason = _ADD_REASONS.get(exc.code)
        if reason is None:
            raise
        raise LinkError(exc.code, exc.status, reason, **exc.params) from exc
