"""Password hashing and session tokens.

Uses `hashlib.scrypt` from the standard library rather than bcrypt or argon2.
scrypt is a memory-hard KDF designed for exactly this, it is in CPython by
default, and it keeps a password-hashing dependency out of an image that is
being kept deliberately small.

Nothing here stores or logs a plaintext password. `verify_password` is
constant-time, so a wrong password cannot be distinguished by timing.
"""

from __future__ import annotations

import base64
import hashlib
import secrets

#: scrypt cost parameters. n is the work factor; 2**14 keeps a login around
#: 50-100 ms on the 0.1 CPU of a free instance, which is slow enough to make
#: offline cracking expensive and fast enough not to be felt.
_N = 2**14
_R = 8
_P = 1
_SALT_BYTES = 16
_KEY_BYTES = 32

_PREFIX = "scrypt"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=_KEY_BYTES,
        # scrypt needs maxmem raised above the default for these parameters.
        maxmem=132 * 1024 * 1024,
    )


def hash_password(password: str) -> str:
    """Return a self-describing hash: the parameters travel with the digest,
    so raising the work factor later does not invalidate existing users."""
    if not password:
        raise ValueError("Password must not be empty")
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = _derive(password, salt, _N, _R, _P)
    return f"{_PREFIX}${_N}${_R}${_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = encoded.split("$")
        if scheme != _PREFIX:
            return False
        expected = _unb64(digest)
        actual = _derive(password, _unb64(salt), int(n), int(r), int(p))
    except (ValueError, TypeError):
        return False
    return secrets.compare_digest(actual, expected)


def new_session_token() -> str:
    """An opaque, unguessable session id.

    Opaque and stored server-side rather than a signed JWT: a session row can
    be deleted, so logout and revocation actually take effect immediately.
    """
    return secrets.token_urlsafe(32)


def hash_session_token(token: str) -> str:
    """Sessions are stored hashed, so a database leak does not hand over live
    sessions. A single SHA-256 is right here -- the token is already 256 bits
    of entropy, so there is nothing to brute-force."""
    return hashlib.sha256(token.encode("ascii")).hexdigest()
