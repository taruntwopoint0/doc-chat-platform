"""The workspace list: everything a dashboard card shows, in one request."""

from __future__ import annotations

import os
from datetime import datetime
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update

from app.main import create_app

TOKEN = os.environ["ADMIN_TOKEN"]
AUTH = {"X-Admin-Token": TOKEN}
MD = b"# Runbook\n\nEscalate a P1 within 15 minutes.\n"


@pytest.fixture
async def client(database_url):
    # No lifespan: the worker must not start and drain the queue mid-test.
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def make_workspace(client, name="Test") -> dict:
    response = await client.post(
        "/api/workspaces",
        json={"name": name, "slug": f"ws-{uuid4().hex[:8]}"},
        headers=AUTH,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def cards(client) -> dict[str, dict]:
    response = await client.get("/api/workspaces", headers=AUTH)
    assert response.status_code == 200, response.text
    return {card["id"]: card for card in response.json()}


async def test_an_empty_workspace_card_has_zero_counts(client):
    workspace = await make_workspace(client)
    card = (await cards(client))[workspace["id"]]

    assert card["documents"]["total"] == 0
    assert card["documents"]["chunk_count"] == 0
    assert card["top_topics"] == []
    assert card["document_types"] == []
    assert card["last_activity_at"] == card["created_at"]


async def test_counts_belong_to_their_own_workspace(client):
    busy = await make_workspace(client, "Busy")
    idle = await make_workspace(client, "Idle")
    upload = await client.post(
        f"/api/workspaces/{busy['id']}/documents",
        files={"file": ("runbook.md", MD, "text/markdown")},
        headers=AUTH,
    )
    assert upload.status_code == 202, upload.text

    listed = await cards(client)
    assert listed[busy["id"]]["documents"]["total"] == 1
    assert listed[busy["id"]]["documents"]["in_progress"] == 1
    assert listed[idle["id"]]["documents"]["total"] == 0

    busy_card = listed[busy["id"]]
    assert datetime.fromisoformat(busy_card["last_activity_at"]) > datetime.fromisoformat(
        busy_card["created_at"]
    ), "adding a document counts as activity"


async def test_topics_are_the_most_frequent_first(client):
    from app.db import session_scope
    from app.models.workspace import Workspace

    workspace = await make_workspace(client)
    profile = {
        "topics": {"leave": 1, "escalation": 4, "expenses": 2, "a": 1, "b": 1, "c": 1},
        "document_types": {"runbook": 3, "policy": 1},
    }
    async with session_scope() as session:
        await session.execute(
            update(Workspace)
            .where(Workspace.id == UUID(workspace["id"]))
            .values(profile=profile)
        )
        await session.commit()

    card = (await cards(client))[workspace["id"]]
    assert card["top_topics"][:2] == ["escalation", "expenses"]
    assert len(card["top_topics"]) == 5
    assert card["document_types"] == ["runbook", "policy"]
