"""Exponential backoff for provider calls.

Shared by the Gemini implementations. Kept provider-agnostic -- it classifies on
HTTP status codes and error-message shape rather than importing an SDK's
exception hierarchy, so a new provider reuses it unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: Rate limit, and the transient server-side failures worth another attempt.
_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
_RETRYABLE_TEXT = (
    "rate limit",
    "resource exhausted",
    "resource_exhausted",
    "deadline exceeded",
    "unavailable",
    "internal error",
    "overloaded",
    "timeout",
    "connection reset",
)


#: Transport failures, matched by class name so this module needs no HTTP
#: client import. httpx raises several of these with an empty message, so the
#: text match below never sees them.
_RETRYABLE_TYPES = {
    "ReadError",
    "WriteError",
    "ConnectError",
    "CloseError",
    "ConnectTimeout",
    "ReadTimeout",
    "WriteTimeout",
    "PoolTimeout",
    "NetworkError",
    "ProxyError",
    "RemoteProtocolError",
    "TransportError",
    "ServerDisconnectedError",
    "IncompleteRead",
}


def is_retryable(exc: BaseException) -> bool:
    status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if isinstance(status, int) and status in _RETRYABLE_STATUS:
        return True
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int) and status in _RETRYABLE_STATUS:
        return True
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    # Walk the class hierarchy: httpx.ReadError is a TransportError, and either
    # name is enough to classify it as transient.
    if any(base.__name__ in _RETRYABLE_TYPES for base in type(exc).__mro__):
        return True
    message = str(exc).lower()
    return any(token in message for token in _RETRYABLE_TEXT)


class RateLimited(RuntimeError):
    """The provider's quota is exhausted and the caller cannot wait it out.

    Raised instead of sleeping when a user is waiting on the other end: a
    minute-long spinner reads as a hang, whereas "busy, try again in 40s" is
    something a person can act on.
    """

    def __init__(self, retry_after: float | None) -> None:
        self.retry_after = retry_after
        wait = (
            f" Try again in about {max(1, round(retry_after))} seconds."
            if retry_after
            else " Try again in a minute."
        )
        # No quota figure here: it depends on the account's tier, and a number
        # that is right on the free tier is wrong the day billing is enabled.
        super().__init__(f"The AI service is at its request limit right now.{wait}")


#: Set while a person is waiting on the result. Background work leaves it
#: unset and waits out the quota window instead of failing.
_interactive: ContextVar[bool] = ContextVar("provider_call_interactive", default=False)

#: Longest a waiting person is kept waiting for a rate limit to clear.
INTERACTIVE_MAX_WAIT = 8.0
#: Quota windows are a minute; never sleep far past one.
_MAX_SERVER_DELAY = 65.0

_RETRY_IN = re.compile(r"retry in ([0-9.]+)\s*s", re.IGNORECASE)
_RETRY_DELAY = re.compile(r"retryDelay['\"]?\s*[:=]\s*['\"]?([0-9.]+)s", re.IGNORECASE)


@contextmanager
def interactive() -> Iterator[None]:
    """Mark provider calls in this block as having a user waiting on them."""
    token = _interactive.set(True)
    try:
        yield
    finally:
        _interactive.reset(token)


def is_rate_limit(exc: BaseException) -> bool:
    """429, or the gRPC RESOURCE_EXHAUSTED status Gemini also reports.

    Not the bare word "quota": a 403 about an unset quota project mentions it
    too, and reporting a misconfigured key as "busy, try again" would hide it.
    """
    status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if status == 429:
        return True
    text = str(exc).lower()
    return "resource_exhausted" in text or "resource exhausted" in text


def server_retry_after(exc: BaseException) -> float | None:
    """How long the provider asked us to wait, if it said.

    Gemini puts it in the message twice: "Please retry in 53.57s" and a
    RetryInfo `retryDelay: '53s'`. Honouring it matters: with only local
    backoff, five attempts span about fifteen seconds -- well short of a
    one-minute quota window -- so the call gave up while still over quota.
    """
    text = str(exc)
    for pattern in (_RETRY_IN, _RETRY_DELAY):
        match = pattern.search(text)
        if match:
            try:
                return min(float(match.group(1)), _MAX_SERVER_DELAY)
            except ValueError:
                continue
    return None


async def with_backoff(
    operation: Callable[[], Awaitable[T]],
    *,
    max_attempts: int,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    description: str = "provider call",
) -> T:
    """Run `operation`, retrying transient failures with jittered backoff.

    Rate limits wait as long as the provider asked, when it said -- unless a
    person is waiting (see `interactive`), in which case a long wait becomes an
    immediate RateLimited they can act on. Jitter matters: a batch of embedding
    requests that all hit the same limit would otherwise retry in lockstep and
    trip it again.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return await operation()
        except Exception as exc:
            limited = is_rate_limit(exc)
            requested = server_retry_after(exc) if limited else None

            if limited and _interactive.get():
                if requested is None or requested > INTERACTIVE_MAX_WAIT or attempt >= 2:
                    raise RateLimited(requested) from exc

            if attempt >= max_attempts or not is_retryable(exc):
                if limited:
                    raise RateLimited(requested) from exc
                raise

            if requested is not None:
                # The provider named its window; a shorter guess only fails again.
                delay = requested + random.uniform(0.5, 2.0)
            else:
                delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                delay *= 0.5 + random.random()
            logger.warning(
                "%s failed (attempt %d/%d): %s -- retrying in %.1fs",
                description,
                attempt,
                max_attempts,
                _summarise(exc),
                delay,
            )
            await asyncio.sleep(delay)


def _summarise(exc: BaseException) -> str:
    """First line only. Gemini's 429 body is a kilobyte of JSON, logged once per
    attempt, which buried everything else in the server log."""
    text = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
    return text[:200]
