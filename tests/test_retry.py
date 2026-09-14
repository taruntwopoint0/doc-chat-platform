"""Retry classification.

Getting this wrong is expensive in both directions: retrying a real bug wastes
five attempts and buries the cause, while not retrying a transient network blip
fails a document that would have succeeded a second later.
"""

from __future__ import annotations

import httpx
import pytest

from app.implementations.retry import is_retryable, with_backoff

pytestmark = pytest.mark.no_db


class _Status(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(f"http {code}")
        self.code = code


@pytest.mark.parametrize(
    "exc",
    [
        # httpx raises several of these with an empty message, so they are
        # classified by type rather than by text.
        httpx.ReadError(""),
        httpx.ConnectError(""),
        httpx.ConnectTimeout(""),
        httpx.RemoteProtocolError(""),
        TimeoutError(),
        ConnectionResetError(),
        _Status(429),
        _Status(503),
        RuntimeError("429 RESOURCE_EXHAUSTED: rate limit exceeded"),
        RuntimeError("The model is overloaded. Please try again later."),
    ],
)
def test_transient_failures_are_retried(exc):
    assert is_retryable(exc) is True


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("auto_truncate parameter is only supported in ... mode"),
        _Status(400),
        _Status(401),
        _Status(404),
        RuntimeError("Embedder returned 1 vectors for 2 inputs"),
        KeyError("document_type"),
    ],
)
def test_real_bugs_are_not_retried(exc):
    """A programming error must surface immediately, not after five attempts."""
    assert is_retryable(exc) is False


async def test_with_backoff_retries_then_succeeds():
    attempts = {"n": 0}

    async def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise httpx.ReadError("")
        return "ok"

    result = await with_backoff(flaky, max_attempts=5, base_delay=0.001)
    assert result == "ok"
    assert attempts["n"] == 3


async def test_with_backoff_gives_up_after_max_attempts():
    attempts = {"n": 0}

    async def always_failing():
        attempts["n"] += 1
        raise httpx.ReadError("")

    with pytest.raises(httpx.ReadError):
        await with_backoff(always_failing, max_attempts=3, base_delay=0.001)
    assert attempts["n"] == 3


async def test_a_non_transient_error_is_raised_on_the_first_attempt():
    attempts = {"n": 0}

    async def broken():
        attempts["n"] += 1
        raise ValueError("unsupported parameter")

    with pytest.raises(ValueError):
        await with_backoff(broken, max_attempts=5, base_delay=0.001)
    assert attempts["n"] == 1, "a real bug was retried"
