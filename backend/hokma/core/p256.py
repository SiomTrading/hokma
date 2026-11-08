"""ECDSA P-256 / SHA-256 signature verification in plain Python integers.

The remembered session needs to check one signature per page load and nothing else, so the engine does it here
instead of depending on a cryptography package. Only verification exists: the engine never holds a private key.

The browser (WebCrypto) delivers a signature as the raw 64 bytes `r || s` and a public key as the two 32 byte
coordinates `x` and `y`. Both shapes are what this module takes.

Checks done before the arithmetic: both coordinates are below the field prime, the point satisfies the curve
equation (so it is not on another curve) and is not the point at infinity, and `1 <= r < n`, `1 <= s < n`.
Inverses use `pow(value, -1, modulus)`. This is a verifier: it handles public data only, so no constant-time
arithmetic is needed.
"""
from __future__ import annotations

import base64
import binascii
import hashlib

# NIST P-256 (secp256r1) domain parameters.
P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
A = P - 3
B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
GX = 0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296
GY = 0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5

Point = tuple[int, int] | None                      # None is the point at infinity
COORDINATE_BYTES = 32
SIGNATURE_BYTES = 64


def is_on_curve(x: int, y: int) -> bool:
    """True when (x, y) is a finite point of P-256."""
    if not (0 <= x < P and 0 <= y < P):
        return False
    return (y * y - (x * x * x + A * x + B)) % P == 0


def _add(p1: Point, p2: Point) -> Point:
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2:
        if (y1 + y2) % P == 0 or y1 == 0:
            return None
        slope = (3 * x1 * x1 + A) * pow(2 * y1, -1, P) % P
    else:
        slope = (y2 - y1) * pow(x2 - x1, -1, P) % P
    x3 = (slope * slope - x1 - x2) % P
    return x3, (slope * (x1 - x3) - y1) % P


def _multiply(k: int, point: Point) -> Point:
    result: Point = None
    addend = point
    while k:
        if k & 1:
            result = _add(result, addend)
        addend = _add(addend, addend)
        k >>= 1
    return result


def verify(x: int, y: int, message: bytes, r: int, s: int) -> bool:
    """True when (r, s) is a valid ECDSA signature of `message` (hashed with SHA-256) under the public key (x, y)."""
    if not is_on_curve(x, y):
        return False
    if not (1 <= r < N and 1 <= s < N):
        return False
    z = int.from_bytes(hashlib.sha256(message).digest(), "big")      # 256 bit hash, no truncation needed for P-256
    w = pow(s, -1, N)
    point = _add(_multiply(z * w % N, (GX, GY)), _multiply(r * w % N, (x, y)))
    if point is None:
        return False
    return point[0] % N == r


def b64url_decode(text: str, size: int) -> bytes | None:
    """`size` bytes from unpadded base64url, or None for anything else (wrong alphabet, wrong length)."""
    if not isinstance(text, str) or len(text) != -(-size * 4 // 3) or not text.isascii():
        return None
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        return None
    if len(raw) != size or base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != text:
        return None                                   # non canonical encodings are not accepted
    return raw


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def public_key_is_valid(x_b64: str, y_b64: str) -> bool:
    """The two coordinates are well formed base64url of 32 bytes and describe a finite point of the curve."""
    x, y = b64url_decode(x_b64, COORDINATE_BYTES), b64url_decode(y_b64, COORDINATE_BYTES)
    return x is not None and y is not None and is_on_curve(int.from_bytes(x, "big"), int.from_bytes(y, "big"))


def verify_raw(x_b64: str, y_b64: str, message: bytes, signature_b64: str) -> bool:
    """`verify` with the shapes the browser delivers: base64url coordinates and the 64 byte `r || s` signature."""
    x, y = b64url_decode(x_b64, COORDINATE_BYTES), b64url_decode(y_b64, COORDINATE_BYTES)
    signature = b64url_decode(signature_b64, SIGNATURE_BYTES)
    if x is None or y is None or signature is None:
        return False
    return verify(int.from_bytes(x, "big"), int.from_bytes(y, "big"), message,
                  int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big"))
