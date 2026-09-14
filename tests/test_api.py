"""API surface: auth, the tenant boundary, and the upload contract."""

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
    # No lifespan: the worker must not start and drain the queue mid-test.
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


# --- authentication ------------------------------------------------------


async def test_admin_routes_reject_a_missing_token(client):
    response = await client.post("/api/workspaces", json={"name": "x", "slug": "x"})
    assert response.status_code == 401
    assert "Bearer" in response.headers.get("WWW-Authenticate", "")


async def test_admin_routes_reject_a_wrong_token(client):
    """A wrong token and a missing one answer the same 401 on purpose:
    distinguishing them tells an attacker when a token exists."""
    response = await client.post(
        "/api/workspaces",
        json={"name": "x", "slug": "x"},
        headers={"X-Admin-Token": "not-the-token"},
    )
    assert response.status_code == 401


async def test_bearer_and_header_forms_both_work(client):
    a = await client.post(
        "/api/workspaces",
        json={"name": "A", "slug": f"a-{uuid4().hex[:8]}"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    b = await client.post(
        "/api/workspaces",
        json={"name": "B", "slug": f"b-{uuid4().hex[:8]}"},
        headers={"X-Admin-Token": TOKEN},
    )
    assert a.status_code == 201 and b.status_code == 201


async def test_health_needs_no_token(client):
    response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["pending_jobs"] == 0


# --- workspaces ----------------------------------------------------------


async def test_a_new_workspace_has_an_empty_profile(client):
    workspace = await make_workspace(client)
    response = await client.get(f"/api/workspaces/{workspace['id']}", headers=AUTH)
    body = response.json()

    assert body["profile"] == {}, "no domain vocabulary is seeded"
    assert body["documents"]["total"] == 0


async def test_duplicate_slugs_are_rejected(client):
    slug = f"dup-{uuid4().hex[:8]}"
    await make_workspace(client, slug)
    response = await client.post(
        "/api/workspaces", json={"name": "Second", "slug": slug}, headers=AUTH
    )
    assert response.status_code == 409


async def test_invalid_slugs_are_rejected(client):
    response = await client.post(
        "/api/workspaces", json={"name": "x", "slug": "Not A Slug"}, headers=AUTH
    )
    assert response.status_code == 422


async def test_an_unknown_workspace_is_a_404(client):
    response = await client.get(f"/api/workspaces/{uuid4()}", headers=AUTH)
    assert response.status_code == 404


# --- uploads -------------------------------------------------------------


async def test_upload_returns_a_job_id_immediately(client):
    workspace = await make_workspace(client)
    response = await client.post(
        f"/api/workspaces/{workspace['id']}/documents",
        files={"file": ("runbook.md", MD, "text/markdown")},
        headers=AUTH,
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["job_id"] is not None
    assert body["status"] == "pending"
    assert body["version_label"] == "v1"


async def test_an_unsupported_type_is_415_and_lists_what_is_accepted(client):
    workspace = await make_workspace(client)
    response = await client.post(
        f"/api/workspaces/{workspace['id']}/documents",
        files={"file": ("archive.tar", b"\x00\x01\x02\xff\xfe\x00", "application/x-tar")},
        headers=AUTH,
    )
    assert response.status_code == 415
    assert ".pdf" in response.json()["detail"]


async def test_an_empty_file_is_400(client):
    workspace = await make_workspace(client)
    response = await client.post(
        f"/api/workspaces/{workspace['id']}/documents",
        files={"file": ("empty.txt", b"", "text/plain")},
        headers=AUTH,
    )
    assert response.status_code == 400


async def test_a_duplicate_upload_reports_that_it_was_skipped(client):
    workspace = await make_workspace(client)
    url = f"/api/workspaces/{workspace['id']}/documents"
    files = {"file": ("runbook.md", MD, "text/markdown")}

    first = (await client.post(url, files=files, headers=AUTH)).json()
    # Mark it indexed; deduplication only applies to documents that are ready.
    from sqlalchemy import update

    from app.db import session_scope
    from app.models.document import Document

    async with session_scope() as s:
        await s.execute(
            update(Document).where(Document.id == first["document_id"]).values(
                status="ready"
            )
        )

    second = (await client.post(url, files=files, headers=AUTH)).json()
    assert second["deduplicated"] is True
    assert second["job_id"] is None


# --- listing, jobs, delete, reindex --------------------------------------


async def test_documents_list_with_status_and_chunk_counts(client):
    workspace = await make_workspace(client)
    await client.post(
        f"/api/workspaces/{workspace['id']}/documents",
        files={"file": ("runbook.md", MD, "text/markdown")},
        headers=AUTH,
    )
    response = await client.get(
        f"/api/workspaces/{workspace['id']}/documents", headers=AUTH
    )
    assert response.status_code == 200
    items = response.json()
    assert len(items) == 1
    assert items[0]["filename"] == "runbook.md"
    assert items[0]["status"] == "pending"
    assert items[0]["chunk_count"] == 0


async def test_job_status_reports_stage_and_progress(client):
    workspace = await make_workspace(client)
    upload = (
        await client.post(
            f"/api/workspaces/{workspace['id']}/documents",
            files={"file": ("runbook.md", MD, "text/markdown")},
            headers=AUTH,
        )
    ).json()

    response = await client.get(f"/api/jobs/{upload['job_id']}", headers=AUTH)
    assert response.status_code == 200
    job = response.json()
    assert job["document_id"] == upload["document_id"]
    assert job["stage"] == "pending"
    assert job["progress_pct"] == 0
    assert job["checkpoint"] == {}


async def test_an_unknown_job_is_a_404(client):
    response = await client.get(f"/api/jobs/{uuid4()}", headers=AUTH)
    assert response.status_code == 404


async def test_delete_removes_the_document(client):
    workspace = await make_workspace(client)
    upload = (
        await client.post(
            f"/api/workspaces/{workspace['id']}/documents",
            files={"file": ("runbook.md", MD, "text/markdown")},
            headers=AUTH,
        )
    ).json()

    response = await client.delete(
        f"/api/documents/{upload['document_id']}", headers=AUTH
    )
    assert response.status_code == 200

    listed = (
        await client.get(f"/api/workspaces/{workspace['id']}/documents", headers=AUTH)
    ).json()
    assert listed == []


async def test_reindex_queues_every_document(client):
    workspace = await make_workspace(client)
    for name in ("a.md", "b.md"):
        await client.post(
            f"/api/workspaces/{workspace['id']}/documents",
            files={"file": (name, MD + name.encode(), "text/markdown")},
            headers=AUTH,
        )

    response = await client.post(
        f"/api/workspaces/{workspace['id']}/reindex", headers=AUTH
    )
    assert response.status_code == 202
    assert response.json()["documents_queued"] == 2


async def test_documents_do_not_leak_between_workspaces(client):
    one = await make_workspace(client)
    two = await make_workspace(client)
    await client.post(
        f"/api/workspaces/{one['id']}/documents",
        files={"file": ("runbook.md", MD, "text/markdown")},
        headers=AUTH,
    )

    listed = (
        await client.get(f"/api/workspaces/{two['id']}/documents", headers=AUTH)
    ).json()
    assert listed == []
