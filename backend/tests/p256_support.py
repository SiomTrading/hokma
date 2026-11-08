"""A browser stand-in for the remembered-session tests: an ECDSA P-256 key and a signer written with the same integer
arithmetic as the verifier, so these tests need no cryptography package. (test_p256.py checks the verifier against
`cryptography` when it is installed.)"""
import hashlib
import secrets

from hokma.core import p256


class Browser:
    """What the page keeps: a private key that never leaves it. `public` is what the engine stores."""

    def __init__(self) -> None:
        self.d = secrets.randbelow(p256.N - 1) + 1
        x, y = p256._multiply(self.d, (p256.GX, p256.GY))
        self.public = (p256.b64url_encode(x.to_bytes(32, "big")), p256.b64url_encode(y.to_bytes(32, "big")))

    def sign(self, message: bytes) -> str:
        z = int.from_bytes(hashlib.sha256(message).digest(), "big")
        while True:
            k = secrets.randbelow(p256.N - 1) + 1
            r = p256._multiply(k, (p256.GX, p256.GY))[0] % p256.N
            s = pow(k, -1, p256.N) * (z + r * self.d) % p256.N
            if r and s:
                return p256.b64url_encode(r.to_bytes(32, "big") + s.to_bytes(32, "big"))

    def public_key_body(self) -> dict:
        return {"x": self.public[0], "y": self.public[1]}

    def signature_for(self, challenge: str, nonce: str, origin: str = "http://hokma.localhost:8765") -> str:
        return self.sign(f"hokma-resume:{origin}:{challenge}:{nonce}".encode("utf-8"))
