"""RFC 6238 TOTP with the Python standard library. No SMS/email dependency and no external package."""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _normalise(secret: str) -> bytes:
    pad = "=" * ((8 - len(secret) % 8) % 8)
    return base64.b32decode(secret.upper() + pad)


def _at(secret: str, counter: int) -> str:
    digest = hmac.new(_normalise(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return f"{code % 1_000_000:06d}"


def now(secret: str) -> str:
    """Current six-digit code, useful for tests and CLI tooling."""
    return _at(secret, int(time.time() // 30))


def verify(secret: str, code: str, at: int | None = None, window: int = 1) -> bool:
    if not secret or not code or not code.isdigit():
        return False
    counter = int((at if at is not None else time.time()) // 30)
    return any(hmac.compare_digest(_at(secret, counter + drift), code) for drift in range(-window, window + 1))


def provisioning_uri(secret: str, email: str, issuer: str = "Orizon Nimasa") -> str:
    label = quote(f"{issuer}:{email}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}&algorithm=SHA1&digits=6&period=30"
