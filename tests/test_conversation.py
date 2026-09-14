"""Conversational messages: "hi" should get a useful reply, not a refusal.

The risk runs both ways. Too narrow, and a greeting is told the documents do
not cover it. Too broad, and a real question gets a canned reply instead of an
answer from the sources -- which is the worse failure for a grounded assistant.
Both directions are pinned here.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app
from app.pipeline.conversation import classify, reply

AUTH = {"X-Admin-Token": os.environ["ADMIN_TOKEN"]}


# --- classification (pure) -----------------------------------------------


@pytest.mark.no_db
@pytest.mark.parametrize(
    "text,kind",
    [
        ("hi", "greeting"),
        ("Hi!", "greeting"),
        ("  HELLO  ", "greeting"),
        ("hey there", "greeting"),
        ("good morning", "greeting"),
        ("thanks", "thanks"),
        ("Thank you!", "thanks"),
        ("ok", "ack"),
        ("got it.", "ack"),
        ("bye", "farewell"),
        ("help", "meta"),
        ("what documents do you have?", "meta"),
    ],
)
def test_conversational_messages_are_recognised(text, kind):
    assert classify(text) == kind


@pytest.mark.no_db
@pytest.mark.parametrize(
    "text",
    [
        # A greeting in front of a real question is still a real question.
        "hi, how do I escalate a P1?",
        "hello what is the retention period for contracts",
        "how do I escalate a P1?",
        "thanks to the policy, who approves a change?",
        "what is the capital of France?",
        # Ambiguous on purpose: in a corpus about a product, these are real
        # questions about that product, so they must reach retrieval.
        "what can you do?",
        "who are you?",
        "ok so what does the escalation path say",
    ],
)
def test_real_questions_are_left_for_retrieval(text):
    assert classify(text) is None, f"{text!r} was swallowed as small talk"


# --- replies (pure) ------------------------------------------------------


@pytest.mark.no_db
def test_a_greeting_describes_what_the_workspace_contains():
    answer = reply(
        "greeting",
        ready_filenames=["handbook.docx", "runbook.pdf"],
        topics=["escalation", "incident triage", "SLA"],
    )
    assert answer.text.startswith("Hi.")
    assert "handbook.docx" in answer.text and "runbook.pdf" in answer.text
    assert "escalation" in answer.text
    assert answer.refused is False
    assert answer.citations == []


@pytest.mark.no_db
def test_an_empty_workspace_says_to_upload_something():
    answer = reply("greeting", ready_filenames=[], topics=[])
    assert "no documents yet" in answer.text
    assert answer.refused is False


@pytest.mark.no_db
def test_documents_still_processing_are_mentioned():
    answer = reply("greeting", ready_filenames=[], topics=[], processing=2)
    assert "2 document(s) are still being processed" in answer.text


@pytest.mark.no_db
def test_only_a_greeting_is_greeted_back():
    """A real question asked of an empty workspace must not open with 'Hi.'"""
    answer = reply(None, ready_filenames=[], topics=[])
    assert not answer.text.startswith("Hi")
    assert "no documents yet" in answer.text


@pytest.mark.no_db
def test_long_document_lists_are_truncated():
    names = [f"doc{i}.pdf" for i in range(12)]
    answer = reply("greeting", ready_filenames=names, topics=[])
    assert "and 7 more" in answer.text
    assert "doc11.pdf" not in answer.text


@pytest.mark.no_db
@pytest.mark.parametrize("kind", ["thanks", "farewell", "ack"])
def test_short_acknowledgements_get_short_replies(kind):
    answer = reply(kind, ready_filenames=["a.md"], topics=["x"])
    assert answer.text
    assert "a.md" not in answer.text, "a thank-you should not recite the corpus"


# --- through the API -----------------------------------------------------


@pytest.fixture
async def client(database_url):
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _workspace(client) -> dict:
    return (
        await client.post(
            "/api/workspaces",
            json={"name": "W", "slug": f"w-{uuid4().hex[:8]}"},
            headers=AUTH,
        )
    ).json()


async def test_hi_to_an_empty_workspace_is_not_a_refusal(client):
    workspace = await _workspace(client)
    response = await client.post(
        f"/api/workspaces/{workspace['id']}/chat",
        json={"question": "hi"},
        headers=AUTH,
    )
    body = response.json()
    assert response.status_code == 200
    assert body["refused"] is False
    assert "no documents yet" in body["answer"]
    assert "do not cover" not in body["answer"]


async def test_hi_does_not_touch_the_llm_or_the_index(client, monkeypatch):
    """Instant and free: no embedding call, no model call."""
    from app.api import chat as chat_module

    async def explode(*args, **kwargs):
        raise AssertionError("a greeting reached retrieval or the model")

    monkeypatch.setattr(chat_module, "retrieve", explode)
    monkeypatch.setattr(chat_module, "answer_question", explode)

    workspace = await _workspace(client)
    response = await client.post(
        f"/api/workspaces/{workspace['id']}/chat",
        json={"question": "hello"},
        headers=AUTH,
    )
    assert response.status_code == 200


async def test_a_real_question_to_an_empty_workspace_says_to_upload(client):
    workspace = await _workspace(client)
    body = (
        await client.post(
            f"/api/workspaces/{workspace['id']}/chat",
            json={"question": "what is the retention period?"},
            headers=AUTH,
        )
    ).json()
    assert body["refused"] is False
    assert "no documents yet" in body["answer"]
    assert not body["answer"].startswith("Hi")
