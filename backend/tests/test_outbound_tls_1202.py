"""Bug 1202: outbound HTTPS trusts the operating system store (truststore), then certifi, then OpenSSL; verification is
never turned off; every internet client goes through `hokma.core.tls`."""
import datetime
import http.server
import ipaddress
import re
import ssl
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from hokma.core import tls
from hokma.i18n import t, use_language

SOURCE = Path(__file__).resolve().parents[1] / "hokma"


def _verified(ctx) -> bool:
    return ctx.check_hostname is True and ctx.verify_mode == ssl.CERT_REQUIRED


# ------------------------------------------------------------------------------------------------ the choice

def test_prefers_truststore():
    truststore = pytest.importorskip("truststore")
    ctx = tls.outbound_context()
    assert isinstance(ctx, truststore.SSLContext) and _verified(ctx)


def test_falls_back_to_certifi_when_truststore_is_missing(monkeypatch):
    certifi = pytest.importorskip("certifi")
    monkeypatch.setitem(sys.modules, "truststore", None)             # import raises ImportError
    seen = []
    real = ssl.create_default_context
    monkeypatch.setattr(ssl, "create_default_context", lambda *a, **k: seen.append(k.get("cafile")) or real(*a, **k))
    ctx = tls.outbound_context()
    assert seen == [certifi.where()] and _verified(ctx)


def test_falls_back_to_certifi_when_truststore_fails_to_build(monkeypatch):
    pytest.importorskip("certifi")
    truststore = pytest.importorskip("truststore")

    def broken(*a, **k):
        raise OSError("no system store")

    real_class = truststore.SSLContext
    monkeypatch.setattr(truststore, "SSLContext", broken)
    ctx = tls.outbound_context()
    assert not isinstance(ctx, real_class) and _verified(ctx)


def test_falls_back_to_the_default_context_when_nothing_else_exists(monkeypatch):
    monkeypatch.setitem(sys.modules, "truststore", None)
    monkeypatch.setitem(sys.modules, "certifi", None)
    ctx = tls.outbound_context()
    assert type(ctx) is ssl.SSLContext and _verified(ctx)


def test_verification_is_never_disabled_anywhere_in_the_code():
    bad = re.compile(r"CERT_NONE|_create_unverified_context|check_hostname\s*=\s*False|verify\s*=\s*False|"
                     r"_create_default_https_context\s*=")
    offenders = [str(p.relative_to(SOURCE)) for p in SOURCE.rglob("*.py") if bad.search(p.read_text(encoding="utf-8"))]
    assert offenders == []


# ------------------------------------------------------------------------------------------------ own CA, local TLS

def _make_pki(folder: Path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    now = datetime.datetime.now(datetime.timezone.utc)

    def name(cn):
        return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])

    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = (x509.CertificateBuilder().subject_name(name("Test Inspection CA")).issuer_name(name("Test Inspection CA"))
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=2))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
                                       key_encipherment=False, data_encipherment=False, key_agreement=False,
                                       encipher_only=False, decipher_only=False), critical=True)
          .sign(ca_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (x509.CertificateBuilder().subject_name(name("localhost")).issuer_name(ca.subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=2))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"),
                                                        x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    ca_file, cert_file, key_file = folder / "ca.pem", folder / "server.pem", folder / "server.key"
    ca_file.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return ca_file, cert_file, key_file


@pytest.fixture
def tls_server(tmp_path):
    """A local HTTPS server whose certificate is signed by a CA that is in no store."""
    pytest.importorskip("cryptography")
    ca_file, cert_file, key_file = _make_pki(tmp_path)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(cert_file, key_file)
    server.socket = server_ctx.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"https://localhost:{server.server_address[1]}/", ca_file
    server.shutdown()
    server.server_close()


def _get(url, context):
    with urllib.request.urlopen(url, timeout=10, context=context) as response:
        return response.read()


def test_a_context_that_has_the_company_ca_connects_and_the_openssl_one_without_it_fails(tls_server):
    url, ca_file = tls_server
    assert _get(url, ssl.create_default_context(cafile=str(ca_file))) == b"ok"
    with pytest.raises(urllib.error.URLError) as caught:
        _get(url, ssl.create_default_context(cafile=None, capath=str(ca_file.parent / "empty")))
    assert tls.is_certificate_error(caught.value)


def test_outbound_context_follows_the_system_store_and_still_verifies(tls_server, monkeypatch, tmp_path):
    """On Linux truststore reads OpenSSL's default store, which SSL_CERT_FILE points at: a stand-in for the Windows store."""
    pytest.importorskip("truststore")
    url, ca_file = tls_server
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "nothing.pem"))
    with pytest.raises(urllib.error.URLError) as caught:                 # CA not in the store: refused
        _get(url, tls.outbound_context())
    assert tls.is_certificate_error(caught.value)
    monkeypatch.setenv("SSL_CERT_FILE", str(ca_file))                    # the CA is now in the "system" store
    assert _get(url, tls.outbound_context()) == b"ok"
    wrong_name = url.replace("localhost", "127.0.0.2")                   # host name check stays on
    with pytest.raises((urllib.error.URLError, OSError)):
        _get(wrong_name, tls.outbound_context())


# ------------------------------------------------------------------------------------------------ every client uses it

class _Marker:
    pass


@pytest.fixture
def marker(monkeypatch):
    mark = _Marker()
    monkeypatch.setattr(tls, "outbound_context", lambda: mark)
    return mark


def test_urllib_helpers_hand_the_context_over(marker, monkeypatch):
    seen = {}
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None, context=None: seen.update(ctx=context))
    tls.urlopen("https://example.com/", timeout=5)
    assert seen["ctx"] is marker
    built = tls.opener()
    https = [h for h in built.handlers if isinstance(h, urllib.request.HTTPSHandler)]
    assert len(https) == 1 and https[0]._context is marker


def test_the_update_check_and_download_use_it(marker, monkeypatch):
    from hokma.update import archive, github

    seen = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None, context=None: seen.append(context) or
                        (_ for _ in ()).throw(OSError("stop")))
    with pytest.raises(OSError):
        github._open(urllib.request.Request("https://api.github.com/"))
    with pytest.raises(Exception):
        archive.fetch_archive("https://github.com/x.tar.gz", Path("unused.bin"), "0" * 64)
    assert seen == [marker, marker]


def test_the_web_readers_and_telegram_use_it(monkeypatch):
    from hokma.docs import web
    from hokma.projects import fetch
    from hokma.telegram import glue

    mark = _Marker()
    for module in (web, fetch):
        monkeypatch.setattr(module, "outbound_context", lambda: mark)
    assert web._PinnedHTTPS("example.com", "93.184.216.34", 443)._context is mark
    assert fetch._PinnedHTTPS("example.com", "93.184.216.34", 443)._context is mark
    monkeypatch.setattr(glue, "outbound_context", lambda: ssl.create_default_context())
    ctx = glue.tls12_context()
    assert ctx.maximum_version == ssl.TLSVersion.TLSv1_2 and _verified(ctx)


def test_no_client_builds_its_own_context_or_opens_an_internet_url_by_itself():
    """Guard: a new outbound client has to use `hokma.core.tls`. Local health checks (http://127.0.0.1) are listed."""
    local_only = {"status/plugin_install.py", "update/detached.py", "core/identity.py", "core/launcher.py",
                  "core/supervisor.py"}
    vendored = "telegram/core/"                                         # verbatim copy of the shared module (manifest)
    pattern = re.compile(r"ssl\.create_default_context\(|ssl\.SSLContext\(|urllib\.request\.(urlopen|build_opener)\(|"
                         r"httpx\.(AsyncClient|Client|AsyncHTTPTransport|HTTPTransport)\(|HTTPSConnection\(")
    offenders = []
    for path in sorted(SOURCE.rglob("*.py")):
        rel = path.relative_to(SOURCE).as_posix()
        if rel == "core/tls.py" or rel in local_only or rel.startswith(vendored):
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if (pattern.search(line) and not line.lstrip().startswith("#") and "class _PinnedHTTPS" not in line
                    and "verify=outbound_context()" not in line):
                offenders.append(f"{rel}:{number}: {line.strip()}")
    assert offenders == []


def test_the_pinned_https_connections_take_their_context_from_the_function():
    for name in ("docs/web.py", "projects/fetch.py"):
        text = (SOURCE / name).read_text(encoding="utf-8")
        assert "context=outbound_context()" in text and "create_default_context" not in text


# ------------------------------------------------------------------------------------------------ the message

def test_the_certificate_message_exists_in_both_languages():
    url = "https://github.com/SiomTrading/hokma/releases"
    with use_language("es"):
        es = t("update.github.certificate", url=url)
    with use_language("en"):
        en = t("update.github.certificate", url=url)
    assert es != "update.github.certificate" and en != "update.github.certificate" and es != en
    assert url in es and url in en and "github.com" in es and "\u2014" not in es and "\u2014" not in en
    assert "{" not in es and "{" not in en


def test_a_failed_certificate_shows_the_clear_message_not_the_openssl_text(monkeypatch):
    from hokma.update import github
    from hokma.update.source import UpdateError

    error = ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
    monkeypatch.setattr(github, "_open", lambda request: (_ for _ in ()).throw(urllib.error.URLError(error)))
    monkeypatch.setenv("HOKMA_LANG", "es")
    with pytest.raises(UpdateError) as caught:
        github._get("https://api.github.com/x", None, "application/json", 1000)
    assert str(caught.value) == t("update.github.certificate", url=github.RELEASES_URL)
    assert github.RELEASES_URL in str(caught.value) and "CERTIFICATE_VERIFY_FAILED" not in str(caught.value)


# ------------------------------------------------------------------------------------------------ websockets and voice

def test_the_guard_covers_websockets_and_the_voice_download():
    """Every file that opens a websocket or uses huggingface_hub takes its certificates from `outbound_context`."""
    users = {}
    for path in sorted(SOURCE.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if re.search(r"from websockets[\w.]* import .*\bconnect\b|websockets\.(asyncio\.client\.)?connect\(|"
                     r"huggingface_hub import|import huggingface_hub", text):
            users[path.relative_to(SOURCE).as_posix()] = text
    assert {"market/adapters/ws_base.py", "market/adapters/siomtrading_futures.py", "voice/model.py"} <= set(users)
    offenders = []
    for rel, text in users.items():
        if rel == "market/adapters/siomtrading_futures.py":
            ok = "ssl=self._ssl()" in text
        elif rel == "market/adapters/ws_base.py":
            ok = "ssl=self._ssl()" in text and "outbound_context()" in text
        else:
            ok = "outbound_context" in text
        if not ok:
            offenders.append(rel)
    assert offenders == []


def test_a_wss_address_connects_with_the_outbound_context_and_a_local_ws_one_without(marker):
    from hokma.market.adapters.ws_base import WebSocketSource

    class Source(WebSocketSource):
        pass

    source = Source.__new__(Source)
    source._url_base = "wss://stream.example.com/ws"
    assert source._ssl() is marker
    source._url_base = "ws://127.0.0.1:9/ws"
    assert source._ssl() is None


def test_the_voice_download_client_verifies_with_the_outbound_context(marker, monkeypatch):
    import huggingface_hub

    from hokma.voice import model

    seen = {}
    monkeypatch.setattr(huggingface_hub, "set_client_factory", lambda factory: seen.update(factory=factory))
    model._use_system_certificates()
    client = seen["factory"]()
    try:
        assert client._transport._pool._ssl_context is marker
    finally:
        client.close()
