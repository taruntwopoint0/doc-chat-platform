"""Writes must be committed BEFORE the client is told they succeeded.

In FastAPI the code after `yield` in a dependency runs after the response has
been sent. The session dependency commits there, so a route that relied on it
answered 200/201/202 while its transaction was still open. A client acting on
that answer straight away -- polling a job it was just handed, or reloading the
workspace list after creating one -- could find nothing. And a commit that then
failed had already been reported as a success.

The in-process ASGI test client runs dependency cleanup before it returns, so it
hides this entirely. These tests therefore run a real uvicorn server.
"""

from __future__ import annotations

import asyncio
import os
import socket
from uuid import uuid4

import httpx
import pytest
import uvicorn

from app.main import create_app

AUTH = {"X-Admin-Token": os.environ["ADMIN_TOKEN"]}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def live(database_url):
    """A real server, so responses are sent exactly as in production.

    The session dependency is swapped for one whose teardown commit is slow --
    a stand-in for Render's database sitting a network hop away. That turns an
    intermittent race into a deterministic failure: any route still relying on
    the teardown commit answers before its data exists, every time.
    """
    from app.api.deps import get_session
    from app.db import get_session_factory

    async def slow_teardown_session():
        async with get_session_factory()() as session:
            try:
                yield session
                await asyncio.sleep(0.5)
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app = create_app()
    app.dependency_overrides[get_session] = slow_teardown_session
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                       lifespan="off")
    )
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=30) as c:
        yield c
    server.should_exit = True
    await task


async def test_a_created_workspace_is_visible_the_moment_it_is_returned(live):
    created = await live.post(
        "/api/workspaces",
        json={"name": "W", "slug": f"w-{uuid4().hex[:8]}"},
        headers=AUTH,
    )
    assert created.status_code == 201
    # A second request, on a fresh connection, straight away.
    async with httpx.AsyncClient(base_url=str(live.base_url), timeout=30) as other:
        seen = await other.get(f"/api/workspaces/{created.json()['id']}", headers=AUTH)
    assert seen.status_code == 200, "workspace was reported created before it was saved"


async def test_an_upload_job_can_be_polled_the_moment_it_is_returned(live):
    ws = (
        await live.post(
            "/api/workspaces", json={"name": "W", "slug": f"w-{uuid4().hex[:8]}"},
            headers=AUTH,
        )
    ).json()
    upload = await live.post(
        f"/api/workspaces/{ws['id']}/documents",
        files={"file": ("a.md", b"# Title\n\nBody text.", "text/markdown")},
        headers=AUTH,
    )
    assert upload.status_code == 202
    job = await live.get(f"/api/jobs/{upload.json()['job_id']}", headers=AUTH)
    assert job.status_code == 200, "handed a job id that did not exist yet"


async def test_a_deleted_workspace_is_gone_the_moment_delete_returns(live):
    ws = (
        await live.post(
            "/api/workspaces", json={"name": "W", "slug": f"w-{uuid4().hex[:8]}"},
            headers=AUTH,
        )
    ).json()
    deleted = await live.delete(f"/api/workspaces/{ws['id']}", headers=AUTH)
    assert deleted.status_code == 200
    seen = await live.get(f"/api/workspaces/{ws['id']}", headers=AUTH)
    assert seen.status_code == 404, "delete was reported before it was committed"
