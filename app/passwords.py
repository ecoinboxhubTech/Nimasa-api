"""Password hashing with scrypt (standard library, no extra dependency) and opaque session tokens."""
import base64
import hashlib
import hmac
import os
import secrets

_N, _R, _P = 2**14, 8, 1
MIN_PASSWORD_LENGTH = 12


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, expected = stored.split("$")
        dk = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(dk, base64.b64decode(expected))
    except (ValueError, TypeError):
        return False


# Verified against when the account does not exist, so response time does not reveal which emails are registered.
DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_digest(token: str) -> str:
    """Only the digest is stored, so a database leak does not expose usable session tokens."""
    return hashlib.sha256(token.encode()).hexdigest()
