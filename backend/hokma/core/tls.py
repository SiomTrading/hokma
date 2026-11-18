"""One certificate patch on ssl, never two (bug 1094).

`pip-system-certs` (a Windows dependency of databento in older locks) injects truststore into `ssl` every time Python
starts, and httpcore2 builds its own `truststore.SSLContext`. With both, `SSLContext.verify_mode` calls itself until
RecursionError at the first HTTPS handshake. The lock no longer carries the package, but a venv built before that, or a
package the student installs, can still bring the patch back. `undo_stacked_truststore` removes the global patch and
leaves truststore for the libraries that ask for it by themselves.
"""
from __future__ import annotations

import ssl


def undo_stacked_truststore() -> bool:
    """True when `ssl.SSLContext` was truststore's and has been restored; False when there was nothing to undo."""
    try:
        import truststore
    except ImportError:
        return False
    if ssl.SSLContext is not truststore.SSLContext:
        return False
    truststore.extract_from_ssl()
    return True


def outbound_context() -> ssl.SSLContext:
    """The TLS context for every connection Hokma opens to the internet (bug 1202).

    The Python that uv installs only knows OpenSSL's own certificate list, so an antivirus that inspects HTTPS, a proxy
    or a company root (installed in the operating system store) makes `CERTIFICATE_VERIFY_FAILED`. Order:
    1. `truststore.SSLContext`: the operating system store, per connection, without patching `ssl` (bug 1094);
    2. certifi's bundle, when truststore is missing or cannot be created;
    3. the default OpenSSL context.
    Certificate and host name verification stay on in all three: this function never turns them off.
    """
    try:
        from hokma.core import nativeprobe

        if not nativeprobe.allowed(nativeprobe.TRUSTSTORE):      # its probe child died here: certifi instead
            raise RuntimeError("truststore is off on this computer")
        import truststore

        context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        return _verified(context)
    except Exception:                                   # noqa: BLE001  (any failure to build it falls to the next source)
        pass
    try:
        import certifi

        return _verified(ssl.create_default_context(cafile=certifi.where()))
    except Exception:                                   # noqa: BLE001
        pass
    return _verified(ssl.create_default_context())


def _verified(context: ssl.SSLContext) -> ssl.SSLContext:
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def is_certificate_error(exc: BaseException | None) -> bool:
    """True when `exc`, or what it wraps (urllib puts the SSL error in `.reason`), is a failed certificate check."""
    seen = 0
    while exc is not None and seen < 5:
        if isinstance(exc, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(exc):
            return True
        exc = getattr(exc, "reason", None) if isinstance(getattr(exc, "reason", None), BaseException) else exc.__cause__
        seen += 1
    return False


def urlopen(request, *, timeout: float):
    """`urllib.request.urlopen` for an internet URL, on `outbound_context`."""
    import urllib.request

    return urllib.request.urlopen(request, timeout=timeout, context=outbound_context())


def opener(*handlers):
    """`urllib.request.build_opener` whose https handler uses `outbound_context`."""
    import urllib.request

    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=outbound_context()), *handlers)
