"""Deleting a workspace.

The most destructive action in the product: it takes the documents, the
index and the conversation with it. What matters is that it takes ALL of
them and nothing belonging to anyone else.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app

TOKEN = os.environ["ADMIN_TOKEN"]
AUTH = {"X-Admin-Token": TOKEN}
MD = b"# Runbook\n\nEscalate a P1 within 15 minutes.\n"


@pytest.fixture
async def client(database_url):
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def make_workspace(client, slug=None) -> dict:
    response = await client.post(
        "/api/workspaces",
        json={"name": "Test", "slug": slug or f"ws-{uuid4().hex[:8]}"},
        headers=AUTH,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_delete_workspace_reports_what_it_removed(client):
    workspace = await make_workspace(client)
    await client.post(
        f"/api/workspaces/{workspace['id']}/documents",
        files={"file": ("runbook.md", MD, "text/markdown")},
        headers=AUTH,
    )

    response = await client.delete(
        f"/api/workspaces/{workspace['id']}", headers=AUTH
    )
    assert response.status_code == 200
    body = response.json()
    assert body["documents_deleted"] == 1
    assert body["name"] == "Test"
    assert "cannot" not in body["message"].lower()


async def test_a_deleted_workspace_is_gone_from_the_list(client):
    workspace = await make_workspace(client)
    before = (await client.get("/api/workspaces", headers=AUTH)).json()
    assert any(w["id"] == workspace["id"] for w in before)

    await client.delete(f"/api/workspaces/{workspace['id']}", headers=AUTH)

    after = (await client.get("/api/workspaces", headers=AUTH)).json()
    assert not any(w["id"] == workspace["id"] for w in after)
    assert (
        await client.get(f"/api/workspaces/{workspace['id']}", headers=AUTH)
    ).status_code == 404


async def test_deleting_a_workspace_takes_its_documents_and_chunks(client):
    """Everything cascades, so nothing is orphaned in the tenant's tables."""
    from sqlalchemy import func, select

    from app.db import session_scope
    from app.models.chunk import Chunk
    from app.models.document import Document, DocumentFile
    from app.models.ingest_job import IngestJob

    workspace = await make_workspace(client)
    await client.post(
        f"/api/workspaces/{workspace['id']}/documents",
        files={"file": ("runbook.md", MD, "text/markdown")},
        headers=AUTH,
    )
    await client.delete(f"/api/workspaces/{workspace['id']}", headers=AUTH)

    async with session_scope() as s:
        for model in (Document, DocumentFile, Chunk, IngestJob):
            remaining = (
                await s.execute(
                    select(func.count())
                    .select_from(model)
                    .where(model.workspace_id == workspace["id"])
                )
            ).scalar_one()
            assert remaining == 0, f"{model.__name__} rows outlived the workspace"


async def test_deleting_one_workspace_leaves_the_others_alone(client):
    keep = await make_workspace(client)
    drop = await make_workspace(client)
    await client.post(
        f"/api/workspaces/{keep['id']}/documents",
        files={"file": ("keep.md", MD, "text/markdown")},
        headers=AUTH,
    )

    await client.delete(f"/api/workspaces/{drop['id']}", headers=AUTH)

    docs = (
        await client.get(f"/api/workspaces/{keep['id']}/documents", headers=AUTH)
    ).json()
    assert [d["filename"] for d in docs] == ["keep.md"]


async def test_deleting_an_unknown_workspace_is_a_404(client):
    response = await client.delete(f"/api/workspaces/{uuid4()}", headers=AUTH)
    assert response.status_code == 404
