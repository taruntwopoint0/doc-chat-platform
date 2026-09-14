"""The app as it ships: AUTH_ENABLED=false, no login screen.

Everything still has to work with no cookie and no token, and chat history
still has to belong to somebody.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app
from tests.test_auth_and_history import add_history

MD = b"# Hello\n\nSome body text."


@pytest.fixture
async def client(database_url):
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def no_auth(client):
    """Run the app the way it ships: AUTH_ENABLED=false, no login screen."""
    from app.api.auth import bootstrap_first_user
    from app.config import get_settings

    settings = get_settings()
    original = settings.auth_enabled
    object.__setattr__(settings, "auth_enabled", False)
    await bootstrap_first_user()
    yield client
    object.__setattr__(settings, "auth_enabled", original)


async def test_with_sign_in_off_the_app_opens_without_a_credential(no_auth):
    """No cookie, no token -- the dashboard should just work."""
    response = await no_auth.get("/api/auth/me")
    assert response.status_code == 200
    assert response.json()["username"] == "local"

    assert (await no_auth.get("/api/workspaces")).status_code == 200


async def test_with_sign_in_off_uploading_needs_no_credential(no_auth):
    workspace = (
        await no_auth.post(
            "/api/workspaces", json={"name": "W", "slug": f"w-{uuid4().hex[:8]}"}
        )
    ).json()
    response = await no_auth.post(
        f"/api/workspaces/{workspace['id']}/documents",
        files={"file": ("x.md", MD, "text/markdown")},
    )
    assert response.status_code == 202


async def test_with_sign_in_off_history_still_belongs_to_someone(no_auth):
    """History hangs off a user row, so the implicit local account owns it."""
    workspace = (
        await no_auth.post(
            "/api/workspaces", json={"name": "W", "slug": f"w-{uuid4().hex[:8]}"}
        )
    ).json()

    from app.api.deps import local_user

    user = await local_user()
    assert user is not None
    await add_history(workspace["id"], user.id, [("q", "a")])

    messages = (await no_auth.get(f"/api/workspaces/{workspace['id']}/history")).json()
    assert [m["content"] for m in messages] == ["q", "a"]

    cleared = (
        await no_auth.delete(f"/api/workspaces/{workspace['id']}/history")
    ).json()
    assert cleared["deleted"] == 2


async def test_health_reports_whether_sign_in_is_on(no_auth):
    """The dashboard reads this to decide whether to show a login screen."""
    assert (await no_auth.get("/health")).json()["auth_enabled"] is False
