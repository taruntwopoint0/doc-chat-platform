"""Password hashing and session tokens.

Neither a password nor a live session token may be recoverable from what
is stored, which is what these pin. Pure functions, no database.
"""

from __future__ import annotations

import pytest

from app.security import hash_password, hash_session_token, verify_password

pytestmark = pytest.mark.no_db

PASSWORD = "correct-horse-battery-staple"


def test_a_password_is_not_recoverable_from_its_hash():
    encoded = hash_password(PASSWORD)
    assert PASSWORD not in encoded
    assert verify_password(PASSWORD, encoded) is True
    assert verify_password("wrong", encoded) is False


def test_the_same_password_hashes_differently_every_time():
    """Per-user salt: two people with the same password must not be visibly
    identical in the database."""
    assert hash_password(PASSWORD) != hash_password(PASSWORD)


def test_a_corrupt_hash_fails_closed():
    for broken in ["", "not-a-hash", "scrypt$bad", "bcrypt$1$2$3$4$5"]:
        assert verify_password(PASSWORD, broken) is False


def test_session_tokens_are_stored_hashed():
    from app.security import new_session_token

    token = new_session_token()
    assert len(token) >= 32
    stored = hash_session_token(token)
    assert token not in stored
    assert stored == hash_session_token(token), "hashing must be deterministic"
