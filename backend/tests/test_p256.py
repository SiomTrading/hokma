"""The pure Python ECDSA P-256 verifier, against a fixed vector and against `cryptography`."""
import hashlib

import pytest

from hokma.core import p256
from p256_support import Browser

# RFC 6979 appendix A.2.5: P-256, SHA-256, message "sample".
X = 0x60FED4BA255A9D31C961EB74C6356D68C049B8923B61FA6CE669622E60F29FB6
Y = 0x7903FE1008B8BC99A41AE9E95628BC64F2F1B20C2D7E9F5177A3C294D4462299
R = 0xEFD48B2AACB6A8FD1140DD9CD45E81D69D2C877B56AAF991C34D0EA84EAF3716
S = 0xF7CB1C942D657C41D436C7A1B6E29F65F3E900DBB9AFF4064DC4AB2F843ACDA8


def test_fixed_vector_verifies_and_small_changes_do_not():
    assert p256.verify(X, Y, b"sample", R, S) is True
    assert p256.verify(X, Y, b"sampld", R, S) is False
    assert p256.verify(X, Y, b"sample", R ^ 1, S) is False
    assert p256.verify(X, Y, b"sample", R, S ^ 1) is False
    assert p256.verify(X, Y ^ 1, b"sample", R, S) is False         # not on the curve


def test_range_checks_of_r_and_s():
    for r, s in ((0, S), (R, 0), (p256.N, S), (R, p256.N), (p256.N + R, S), (R, p256.N + S), (-1, S)):
        assert p256.verify(X, Y, b"sample", r, s) is False


def test_keys_off_the_curve_or_at_infinity_are_refused():
    assert not p256.is_on_curve(0, 0) and not p256.is_on_curve(p256.P, Y) and not p256.is_on_curve(X, p256.P + Y)
    assert p256.verify(0, 0, b"sample", R, S) is False
    assert p256.verify(X + p256.P, Y, b"sample", R, S) is False      # coordinates are not reduced for the caller


def test_raw_forms_of_the_browser():
    x, y = (p256.b64url_encode(v.to_bytes(32, "big")) for v in (X, Y))
    signature = p256.b64url_encode(R.to_bytes(32, "big") + S.to_bytes(32, "big"))
    assert p256.public_key_is_valid(x, y) and p256.verify_raw(x, y, b"sample", signature)
    assert not p256.verify_raw(x, y, b"sample", signature[:-1])
    assert not p256.verify_raw(x, y, b"sample", signature + "A")
    assert not p256.verify_raw(x, y, b"sample", "!" * 86)
    assert not p256.verify_raw(x, y, b"sample", "")
    assert not p256.public_key_is_valid(x, x) and not p256.public_key_is_valid(x[:-1], y)
    assert not p256.public_key_is_valid(x + "=", y) and not p256.public_key_is_valid("", "")


def test_the_test_signer_round_trips():
    browser = Browser()
    message = b"hokma-resume:abc:def"
    assert p256.verify_raw(*browser.public, message, browser.sign(message))
    assert not p256.verify_raw(*browser.public, message + b"x", browser.sign(message))
    assert not p256.verify_raw(*Browser().public, message, browser.sign(message))


# ----------------------------------------------------------------------------------------- against cryptography

@pytest.fixture
def lib():
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, utils

    return ec, hashes, utils


def _public(lib, key):
    numbers = key.public_key().public_numbers()
    return numbers.x, numbers.y


def _sign(lib, key, message):
    ec, hashes, utils = lib
    r, s = utils.decode_dss_signature(key.sign(message, ec.ECDSA(hashes.SHA256())))
    return r, s


def test_signatures_made_by_cryptography_verify(lib):
    ec = lib[0]
    for i in range(6):
        key = ec.generate_private_key(ec.SECP256R1())
        message = hashlib.sha256(str(i).encode()).digest() * (i + 1)
        r, s = _sign(lib, key, message)
        assert p256.verify(*_public(lib, key), message, r, s) is True


def test_altered_signature_or_message_is_refused(lib):
    ec = lib[0]
    key = ec.generate_private_key(ec.SECP256R1())
    x, y = _public(lib, key)
    r, s = _sign(lib, key, b"message")
    assert p256.verify(x, y, b"message", r, s)
    assert not p256.verify(x, y, b"messagE", r, s)
    assert not p256.verify(x, y, b"message", r, (s + 1) % p256.N or 1)
    assert not p256.verify(x, y, b"message", (r + 1) % p256.N or 1, s)
    other_x, other_y = _public(lib, ec.generate_private_key(ec.SECP256R1()))
    assert not p256.verify(other_x, other_y, b"message", r, s)


def test_values_out_of_range_are_refused_even_for_a_real_signature(lib):
    ec = lib[0]
    key = ec.generate_private_key(ec.SECP256R1())
    x, y = _public(lib, key)
    r, s = _sign(lib, key, b"m")
    assert not p256.verify(x, y, b"m", 0, s) and not p256.verify(x, y, b"m", r, 0)
    assert not p256.verify(x, y, b"m", p256.N, s) and not p256.verify(x, y, b"m", r + p256.N, s)
    assert not p256.verify(x, y, b"m", r, s + p256.N)


def test_a_key_of_another_curve_is_not_accepted(lib):
    ec = lib[0]
    for curve in (ec.SECP256K1(), ec.SECP384R1()):
        key = ec.generate_private_key(curve)
        x, y = _public(lib, key)
        message = b"message"
        r, s = _sign(lib, key, message)
        assert p256.verify(x, y, message, r % p256.N or 1, s % p256.N or 1) is False
        assert not p256.public_key_is_valid(p256.b64url_encode((x % 2**256).to_bytes(32, "big")),
                                            p256.b64url_encode((y % 2**256).to_bytes(32, "big")))


def test_the_browser_format_matches_webcrypto_raw_signatures(lib):
    """WebCrypto gives r||s, 32 bytes each; cryptography gives DER. The conversion is what the engine reads."""
    ec = lib[0]
    key = ec.generate_private_key(ec.SECP256R1())
    x, y = _public(lib, key)
    r, s = _sign(lib, key, b"hokma-resume:c:n")
    raw = p256.b64url_encode(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    assert p256.verify_raw(p256.b64url_encode(x.to_bytes(32, "big")), p256.b64url_encode(y.to_bytes(32, "big")),
                           b"hokma-resume:c:n", raw)
