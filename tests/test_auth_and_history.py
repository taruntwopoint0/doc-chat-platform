"""Sign-in, authorisation and saved chat history.

Passwords and session tokens are the two things here that must never be
recoverable from the database, and chat history is the one thing that must
never be visible to another user. Those are what these tests pin.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app
from app.security import hash_password

TOKEN = os.environ["ADMIN_TOKEN"]
AUTH = {"X-Admin-Token": TOKEN}
PASSWORD = "correct-horse-battery-staple"
MD = b"# Hello\n\nSome body text."


@pytest.fixture
async def client(database_url):
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def make_user(username: str, password: str = PASSWORD, is_admin: bool = True):
    from app.db import session_scope
    from app.models.account import User

    async with session_scope() as session:
        user = User(
            id=uuid4(),
            username=username,
            display_name=username,
            password_hash=hash_password(password),
            is_admin=is_admin,
        )
        session.add(user)
        await session.flush()
        return user.id


async def sign_in(client, username: str, password: str = PASSWORD):
    return await client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )


# --- sign in -------------------------------------------------------------


async def test_login_sets_a_session_cookie(client):
    await make_user("ada")
    response = await sign_in(client, "ada")
    assert response.status_code == 200
    assert response.json()["username"] == "ada"
    assert "docchat_session" in response.cookies


async def test_the_wrong_password_is_rejected(client):
    await make_user("ada")
    assert (await sign_in(client, "ada", "nope")).status_code == 401


async def test_an_unknown_user_is_rejected(client):
    assert (await sign_in(client, "nobody")).status_code == 401


async def test_usernames_are_case_insensitive(client):
    await make_user("ada")
    assert (await sign_in(client, "ADA")).status_code == 200


async def test_an_inactive_user_cannot_sign_in(client):
    from sqlalchemy import update

    from app.db import session_scope
    from app.models.account import User

    user_id = await make_user("ada")
    async with session_scope() as s:
        await s.execute(update(User).where(User.id == user_id).values(is_active=False))
    assert (await sign_in(client, "ada")).status_code == 401


async def test_me_needs_a_session(client):
    assert (await client.get("/api/auth/me")).status_code == 401
    await make_user("ada")
    await sign_in(client, "ada")
    assert (await client.get("/api/auth/me")).json()["username"] == "ada"


async def test_logout_revokes_the_session_immediately(client):
    await make_user("ada")
    await sign_in(client, "ada")
    assert (await client.get("/api/auth/me")).status_code == 200

    await client.post("/api/auth/logout")
    assert (await client.get("/api/auth/me")).status_code == 401


async def test_the_admin_token_still_works_for_scripts(client):
    """CI and the seed script have no browser to hold a cookie."""
    response = await client.post(
        "/api/workspaces",
        json={"name": "S", "slug": f"s-{uuid4().hex[:8]}"},
        headers=AUTH,
    )
    assert response.status_code == 201


# --- authorisation -------------------------------------------------------


async def test_a_non_admin_cannot_upload(client):
    workspace = (
        await client.post(
            "/api/workspaces",
            json={"name": "W", "slug": f"w-{uuid4().hex[:8]}"},
            headers=AUTH,
        )
    ).json()

    await make_user("reader", is_admin=False)
    await sign_in(client, "reader")
    response = await client.post(
        f"/api/workspaces/{workspace['id']}/documents",
        files={"file": ("x.md", b"# hello\n\nbody text", "text/markdown")},
    )
    assert response.status_code == 403


async def test_a_non_admin_can_still_see_workspaces(client):
    """You must be able to see a workspace to chat with it."""
    await client.post(
        "/api/workspaces",
        json={"name": "W", "slug": f"w-{uuid4().hex[:8]}"},
        headers=AUTH,
    )
    await make_user("reader", is_admin=False)
    await sign_in(client, "reader")
    response = await client.get("/api/workspaces")
    assert response.status_code == 200
    assert len(response.json()) >= 1


# --- chat history --------------------------------------------------------


async def add_history(workspace_id, user_id, pairs):
    from uuid import uuid4 as u4

    from app.db import session_scope
    from app.models.chat import ChatMessage

    async with session_scope() as session:
        for question, answer in pairs:
            session.add(ChatMessage(id=u4(), workspace_id=workspace_id,
                                    user_id=user_id, role="user",
                                    content=question, citations=[], refused=False))
            session.add(ChatMessage(id=u4(), workspace_id=workspace_id,
                                    user_id=user_id, role="assistant",
                                    content=answer, citations=[], refused=False))


async def two_workspaces(client):
    a = (await client.post("/api/workspaces",
                           json={"name": "Compliance", "slug": f"c-{uuid4().hex[:8]}"},
                           headers=AUTH)).json()
    b = (await client.post("/api/workspaces",
                           json={"name": "Onboarding", "slug": f"o-{uuid4().hex[:8]}"},
                           headers=AUTH)).json()
    return a, b


async def test_history_comes_back_in_order(client):
    workspace, _ = await two_workspaces(client)
    user_id = await make_user("ada")
    await sign_in(client, "ada")
    await add_history(workspace["id"], user_id, [("q1", "a1"), ("q2", "a2")])

    messages = (await client.get(f"/api/workspaces/{workspace['id']}/history")).json()
    assert [m["content"] for m in messages] == ["q1", "a1", "q2", "a2"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]


async def test_each_workspace_keeps_its_own_conversation(client):
    """Switching workspace must switch conversation, not merge them."""
    compliance, onboarding = await two_workspaces(client)
    user_id = await make_user("ada")
    await sign_in(client, "ada")
    await add_history(compliance["id"], user_id, [("retention policy?", "seven years")])
    await add_history(onboarding["id"], user_id, [("first day?", "collect laptop")])

    a = (await client.get(f"/api/workspaces/{compliance['id']}/history")).json()
    b = (await client.get(f"/api/workspaces/{onboarding['id']}/history")).json()
    assert [m["content"] for m in a] == ["retention policy?", "seven years"]
    assert [m["content"] for m in b] == ["first day?", "collect laptop"]


async def test_history_is_private_to_its_user(client):
    workspace, _ = await two_workspaces(client)
    ada = await make_user("ada")
    await make_user("bob")
    await add_history(workspace["id"], ada, [("ada's question", "ada's answer")])

    await sign_in(client, "bob")
    assert (await client.get(f"/api/workspaces/{workspace['id']}/history")).json() == []

    await sign_in(client, "ada")
    assert len(
        (await client.get(f"/api/workspaces/{workspace['id']}/history")).json()
    ) == 2


async def test_clearing_removes_only_this_workspace_and_this_user(client):
    compliance, onboarding = await two_workspaces(client)
    ada = await make_user("ada")
    bob = await make_user("bob")
    await add_history(compliance["id"], ada, [("q", "a")])
    await add_history(onboarding["id"], ada, [("q", "a")])
    await add_history(compliance["id"], bob, [("q", "a")])

    await sign_in(client, "ada")
    result = (await client.delete(f"/api/workspaces/{compliance['id']}/history")).json()
    assert result["deleted"] == 2

    assert (await client.get(f"/api/workspaces/{compliance['id']}/history")).json() == []
    assert len(
        (await client.get(f"/api/workspaces/{onboarding['id']}/history")).json()
    ) == 2, "clearing one workspace emptied another"

    await sign_in(client, "bob")
    assert len(
        (await client.get(f"/api/workspaces/{compliance['id']}/history")).json()
    ) == 2, "clearing my history deleted someone else's"


async def test_clearing_history_does_not_touch_the_documents(client):
    """Clearing what you asked is not the same as deleting the corpus."""
    workspace, _ = await two_workspaces(client)
    await client.post(
        f"/api/workspaces/{workspace['id']}/documents",
        files={"file": ("keep.md", b"# Keep\n\nThis must survive.", "text/markdown")},
        headers=AUTH,
    )
    user_id = await make_user("ada")
    await sign_in(client, "ada")
    await add_history(workspace["id"], user_id, [("q", "a")])

    await client.delete(f"/api/workspaces/{workspace['id']}/history")

    docs = (await client.get(f"/api/workspaces/{workspace['id']}/documents")).json()
    assert [d["filename"] for d in docs] == ["keep.md"]


async def test_clearing_an_empty_conversation_is_harmless(client):
    workspace, _ = await two_workspaces(client)
    await make_user("ada")
    await sign_in(client, "ada")
    result = (await client.delete(f"/api/workspaces/{workspace['id']}/history")).json()
    assert result["deleted"] == 0


async def test_a_non_admin_cannot_delete_a_workspace(client):
    """Deleting a corpus is the most destructive action here."""
    workspace = (
        await client.post(
            "/api/workspaces",
            json={"name": "W", "slug": f"w-{uuid4().hex[:8]}"},
            headers=AUTH,
        )
    ).json()

    await make_user("reader", is_admin=False)
    await sign_in(client, "reader")
    assert (
        await client.delete(f"/api/workspaces/{workspace['id']}")
    ).status_code == 403
