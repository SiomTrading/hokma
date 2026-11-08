"""Safe download for "add by link".

Rules, all enforced here and not by the caller:
- `https` only, port 443, no credentials in the URL.
- Every host name is resolved here and every address must be public (no loopback, private, link-local, reserved,
  multicast, or IPv4-mapped forms of those). The connection goes to the address that was checked, not to a second
  lookup, so a name that changes its answer cannot slip through (DNS rebinding).
- Redirects are followed by hand, at most 5, and each hop goes through the same checks.
- At most 25 MB, read in chunks; a declared size above the limit is refused before reading.
- Type allowlist (documents, images, audio, plain data). Web pages are not downloaded: the link is only saved.
  Programs and scripts are refused by content, whatever the declared type says.
"""
import http.client
import ipaddress
import mimetypes
import socket
import ssl
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import unquote, urljoin, urlsplit

from hokma.engine.attachments import MAX_ATTACHMENT_BYTES, safe_name, sniff_mime
from hokma.projects.errors import ProjectError

MAX_REDIRECTS = 5
TIMEOUT_SECONDS = 15
TOTAL_SECONDS = 90
CHUNK = 64 * 1024
USER_AGENT = "Hokma/0.0.6"

PAGE_TYPES = {"text/html", "application/xhtml+xml"}
ALLOWED_TYPES = {
    "application/pdf", "image/png", "image/jpeg", "image/gif", "image/webp", "text/plain", "text/markdown",
    "text/csv", "application/json", "application/msword", "application/vnd.ms-excel", "application/vnd.ms-powerpoint",
    "application/rtf", "text/rtf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.oasis.opendocument.text", "application/vnd.oasis.opendocument.spreadsheet",
}
ALLOWED_PREFIXES = ("audio/",)
GENERIC_TYPES = {"application/octet-stream", "binary/octet-stream", ""}
ALLOWED_EXT = {"pdf", "png", "jpg", "jpeg", "gif", "webp", "txt", "md", "csv", "json", "doc", "docx", "xls", "xlsx",
               "ppt", "pptx", "rtf", "odt", "ods", "mp3", "m4a", "wav", "ogg", "flac", "aac", "opus"}
_EXT_BY_TYPE = {"text/plain": ".txt", "text/markdown": ".md", "text/csv": ".csv", "application/json": ".json",
                "image/jpeg": ".jpg", "application/pdf": ".pdf"}
_PROGRAM_MAGIC = (b"MZ", b"\x7fELF", b"#!", b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe")

Resolver = Callable[[str, int], list[str]]
Connector = Callable[[str, str, int], "http.client.HTTPConnection"]


@dataclass
class Fetched:
    url: str                 # final URL after redirects
    name: str = ""
    mime: str = ""
    data: bytes | None = None   # None: it is a web page (or empty), only the link is kept


def is_public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if isinstance(ip, ipaddress.IPv6Address) and ip.sixtofour is not None:
        ip = ip.sixtofour
    return ip.is_global and not ip.is_multicast


def check_url(url: str) -> tuple[str, str]:
    """(host, request target) of a link that may be fetched, or ProjectError."""
    if not isinstance(url, str) or len(url) > 2000 or any(ord(c) < 33 for c in url):
        raise ProjectError("bad_url", 400)
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError as exc:
        raise ProjectError("bad_url", 400) from exc
    if parts.scheme.lower() != "https" or not parts.hostname:
        raise ProjectError("url_not_https", 400)
    if parts.username is not None or parts.password is not None:
        raise ProjectError("bad_url", 400)
    if port not in (None, 443):
        raise ProjectError("url_port", 400)
    target = parts.path or "/"
    if parts.query:
        target += "?" + parts.query
    return parts.hostname.rstrip(".").lower(), target


def system_resolver(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ProjectError("dns_failed", 502, host=host) from exc
    seen: list[str] = []
    for info in infos:
        address = str(info[4][0])
        if address not in seen:
            seen.append(address)
    return seen


class _PinnedHTTPS(http.client.HTTPSConnection):
    """TLS to the checked address, certificate verified against the original host name."""

    def __init__(self, host: str, address: str, port: int) -> None:
        super().__init__(host, port, timeout=TIMEOUT_SECONDS, context=ssl.create_default_context())
        self._address = address

    def connect(self) -> None:
        sock = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def default_connector(host: str, address: str, port: int) -> http.client.HTTPConnection:
    return _PinnedHTTPS(host, address, port)


def _name_from(url: str, disposition: str | None, mime: str) -> str:
    name = ""
    if disposition and "filename=" in disposition:
        name = disposition.split("filename=", 1)[1].split(";")[0].strip().strip('"')
    if not name:
        name = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    name = safe_name(name) if name else ""
    if name in ("", "file"):
        name = "download"
    if "." not in name:
        name += _EXT_BY_TYPE.get(mime) or mimetypes.guess_extension(mime) or ""
    return name


def _classify(declared: str, name: str, head: bytes) -> str:
    """Final mime to store, or ProjectError when the type is not allowed."""
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if any(head.startswith(magic) for magic in _PROGRAM_MAGIC):
        raise ProjectError("type_blocked", 415)
    if declared in GENERIC_TYPES:
        if ext not in ALLOWED_EXT:
            raise ProjectError("type_blocked", 415)
        return sniff_mime(head, mimetypes.guess_type(name)[0] or "application/octet-stream")
    if declared in ALLOWED_TYPES or declared.startswith(ALLOWED_PREFIXES):
        if declared.startswith("image/") and sniff_mime(head, declared) == "application/octet-stream":
            raise ProjectError("type_blocked", 415)
        return declared
    raise ProjectError("type_blocked", 415)


def fetch(url: str, *, resolver: Resolver | None = None, connector: Connector | None = None,
          max_bytes: int = MAX_ATTACHMENT_BYTES) -> Fetched:
    resolve = resolver or system_resolver
    connect = connector or default_connector
    current = url
    deadline = time.monotonic() + TOTAL_SECONDS
    for _ in range(MAX_REDIRECTS + 1):
        host, target = check_url(current)
        addresses = resolve(host, 443)
        if not addresses or not all(is_public(a) for a in addresses):
            raise ProjectError("url_private", 400)
        response, conn = _open(connect, host, addresses, target)
        try:
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                if not location:
                    raise ProjectError("http_status", 502, code_http=response.status)
                current = urljoin(current, location)
                continue
            if response.status != 200:
                raise ProjectError("http_status", 502, code_http=response.status)
            declared = (response.getheader("Content-Type") or "").split(";")[0].strip().lower()
            if declared in PAGE_TYPES:
                return Fetched(url=current)
            length = response.getheader("Content-Length")
            if length and length.isdigit() and int(length) > max_bytes:
                raise ProjectError("too_large", 413)
            data = bytearray()
            while True:
                chunk = response.read(CHUNK)
                if not chunk:
                    break
                data += chunk
                if len(data) > max_bytes:
                    raise ProjectError("too_large", 413)
                if time.monotonic() > deadline:
                    raise ProjectError("timeout", 504)
            if not data:
                raise ProjectError("empty_download", 502)
            name = _name_from(current, response.getheader("Content-Disposition"), declared)
            mime = _classify(declared, name, bytes(data[:16]))
            return Fetched(url=current, name=name, mime=mime, data=bytes(data))
        finally:
            conn.close()
    raise ProjectError("too_many_redirects", 502)


def _open(connect: Connector, host: str, addresses: list[str], target: str):
    last: Exception | None = None
    for address in addresses:
        conn = connect(host, address, 443)
        try:
            conn.request("GET", target, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity",
                                                 "Accept": "*/*", "Connection": "close"})
            return conn.getresponse(), conn
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            last = exc
            conn.close()
    raise ProjectError("connect_failed", 502, host=host) from last
