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
