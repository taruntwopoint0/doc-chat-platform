"""Provider rate limits.

Found by the production smoke test: the free Gemini tier allows 15 requests a
minute, eight uploads spent it, and the next question failed. Two behaviours
are pinned here:

* background work waits as long as the provider asks, so an upload eventually
  indexes instead of failing inside the quota window;
* a person waiting on an answer gets a fast, readable "busy" instead of a
  minute-long spinner or a raw provider error.
"""

from __future__ import annotations

import pytest

from app.implementations import retry
from app.implementations.retry import (
    RateLimited,
    interactive,
    is_rate_limit,
    server_retry_after,
    with_backoff,
)

pytestmark = pytest.mark.no_db

GEMINI_429 = (
    "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'You exceeded your "
    "current quota. Please retry in 53.571849336s.', 'status': 'RESOURCE_EXHAUSTED', "
    "'details': [{'@type': 'type.googleapis.com/google.rpc.RetryInfo', "
    "'retryDelay': '53s'}]}}"
)


class Provider429(Exception):
    code = 429


# --- reading the provider's instructions ---------------------------------


def test_the_requested_wait_is_read_from_the_error():
    assert server_retry_after(Exception(GEMINI_429)) == pytest.approx(53.57, abs=0.01)


def test_retry_delay_alone_is_understood():
    assert server_retry_after(Exception("RetryInfo retryDelay: '12s'")) == 12


def test_an_absurd_wait_is_capped_at_about_a_minute():
    assert server_retry_after(Exception("Please retry in 9000s")) <= 65


def test_no_hint_means_no_requested_wait():
    assert server_retry_after(Exception("429 slow down")) is None


@pytest.mark.parametrize(
    "exc",
    [Provider429("slow down"), Exception(GEMINI_429), Exception("RESOURCE EXHAUSTED")],
)
def test_rate_limits_are_recognised(exc):
    assert is_rate_limit(exc)


def test_a_403_mentioning_quota_is_not_a_rate_limit():
    """A misconfigured key must surface as itself, not as 'busy, try again'."""
    class Forbidden(Exception):
        code = 403

    assert not is_rate_limit(Forbidden("quota project not set for this API key"))


# --- background work waits it out ---------------------------------------


async def test_background_work_waits_as_long_as_the_provider_asked(monkeypatch):
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(retry.asyncio, "sleep", fake_sleep)
    calls = {"n": 0}

    async def quota_then_ok():
        calls["n"] += 1
        if calls["n"] == 1:
            raise Provider429(GEMINI_429)
        return "indexed"

    assert await with_backoff(quota_then_ok, max_attempts=5) == "indexed"
    assert slept and slept[0] >= 53, f"retried after {slept[0]:.1f}s, inside the window"


async def test_background_work_gives_up_with_a_clear_error_eventually(monkeypatch):
    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr(retry.asyncio, "sleep", fake_sleep)

    async def always_limited():
        raise Provider429(GEMINI_429)

    with pytest.raises(RateLimited):
        await with_backoff(always_limited, max_attempts=3)


# --- a waiting person is answered fast ------------------------------------


async def test_a_waiting_person_is_not_made_to_wait_a_minute(monkeypatch):
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(retry.asyncio, "sleep", fake_sleep)

    async def limited():
        raise Provider429(GEMINI_429)

    with interactive(), pytest.raises(RateLimited) as excinfo:
        await with_backoff(limited, max_attempts=5)
    assert slept == [], "slept while a user was waiting"
    assert excinfo.value.retry_after == pytest.approx(53.57, abs=0.01)


async def test_a_short_requested_wait_is_absorbed_even_when_interactive(monkeypatch):
    """Two seconds is worth waiting for; it saves the user a retry."""
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(retry.asyncio, "sleep", fake_sleep)
    calls = {"n": 0}

    async def briefly_limited():
        calls["n"] += 1
        if calls["n"] == 1:
            raise Provider429("Please retry in 2s")
        return "answer"

    with interactive():
        assert await with_backoff(briefly_limited, max_attempts=5) == "answer"
    assert slept and slept[0] < 5


def test_the_busy_message_is_readable_and_names_no_quota_figure():
    message = str(RateLimited(40.2))
    assert "Try again in about 40 seconds" in message
    assert "15" not in message, "a tier-specific figure goes stale when billing is on"
    assert "{" not in message, "raw provider JSON leaked into the message"


def test_interactive_does_not_leak_out_of_its_block():
    with interactive():
        assert retry._interactive.get() is True
    assert retry._interactive.get() is False
