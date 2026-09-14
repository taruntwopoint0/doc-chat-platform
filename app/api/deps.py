"""Request dependencies: authentication and the database session.

Authentication is one dependency on purpose, so swapping the scheme never
touches a route handler. Two credentials are accepted:

* a **session cookie** from `POST /api/auth/login` -- what the dashboard uses;
* the **shared admin token** -- kept for scripts, tests and CI, which have no
  browser to hold a cookie.

Replacing either with SSO means rewriting `require_user` and nothing else.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from fastapi import Depends, HTTPException, Path, Request, Security, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_session_factory, session_scope
from app.models.account import Session as SessionRow
from app.models.account import User
from app.models.workspace import Workspace
from app.security import hash_session_token

COOKIE_NAME = "docchat_session"

#: The implicit account when sign-in is switched off.
LOCAL_USERNAME = "local"

_ADMIN_TOKEN_HEADER = APIKeyHeader(
    name="X-Admin-Token",
    auto_error=False,
    description="Shared admin token (ADMIN_TOKEN). For scripts and CI.",
)
_BEARER = HTTPBearer(auto_error=False, description="Shared admin token as a bearer.")


@dataclass(frozen=True)
class Principal:
    """Who is making the request."""

    subject: str
    is_admin: bool = True
    #: None when authenticated by the shared token rather than as a person.
    #: Chat history needs a real user to belong to, so it is not saved then.
    user_id: UUID | None = None


async def get_session() -> AsyncIterator[AsyncSession]:
    """A request-scoped session.

    Routes that WRITE must `await session.commit()` themselves before
    returning. FastAPI runs the code after `yield` only once the response has
    been sent, so the commit below would tell the client "done" while the
    transaction is still open -- and report success for a commit that then
    fails. It stays as a safety net and rolls back on error.
    tests/test_commit_before_response.py pins this against a real server.
    """
    async with get_session_factory()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def current_user(request: Request) -> User | None:
    """Resolve the session cookie to a user, or None."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    token_hash = hash_session_token(token)

    async with session_scope() as session:
        row = (
            await session.execute(
                select(SessionRow, User)
                .join(User, User.id == SessionRow.user_id)
                .where(SessionRow.token_hash == token_hash)
            )
        ).first()
        if row is None:
            return None
        session_row, user = row
        if session_row.expires_at <= datetime.now(UTC) or not user.is_active:
            return None
        # Cheap liveness signal; not written on every request in a hot loop,
        # but this app's request rate makes that a non-issue.
        await session.execute(
            update(SessionRow)
            .where(SessionRow.token_hash == token_hash)
            .values(last_seen_at=datetime.now(UTC))
        )
        session.expunge(user)
        return user


def _token_matches(presented: str | None, expected: str) -> bool:
    if not presented or not expected:
        return False
    # Constant-time: a plain == leaks the token's prefix through timing.
    return secrets.compare_digest(presented, expected)


async def local_user() -> User | None:
    """The implicit account used when AUTH_ENABLED is false.

    Chat history belongs to a user row, so with sign-in switched off there
    still has to be one -- everyone is simply the same person.
    """
    async with session_scope() as session:
        user = (
            await session.execute(select(User).where(User.username == LOCAL_USERNAME))
        ).scalar_one_or_none()
        if user is not None:
            session.expunge(user)
        return user


async def require_user(
    request: Request,
    x_admin_token: str | None = Security(_ADMIN_TOKEN_HEADER),
    bearer: HTTPAuthorizationCredentials | None = Security(_BEARER),
    settings: Settings = Depends(get_settings),
) -> Principal:
    """Accept a session cookie or the shared admin token.

    With AUTH_ENABLED false, every request is the local user and nothing is
    checked at all.
    """
    if not settings.auth_enabled:
        user = await local_user()
        return Principal(
            subject=LOCAL_USERNAME,
            is_admin=True,
            user_id=user.id if user else None,
        )

    user = await current_user(request)
    if user is not None:
        return Principal(
            subject=user.username, is_admin=user.is_admin, user_id=user.id
        )

    presented = x_admin_token or (bearer.credentials if bearer else None)
    if _token_matches(presented, settings.admin_token):
        return Principal(subject="admin-token", is_admin=True, user_id=None)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Sign in, or supply the admin token.",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def require_admin(
    principal: Principal = Depends(require_user),
) -> Principal:
    """Uploading, deleting and reindexing are admin-only; chatting is not."""
    if not principal.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This action needs an administrator account.",
        )
    return principal


async def require_workspace(
    workspace_id: UUID = Path(...),
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_user),
) -> Workspace:
    """Resolve the workspace and confirm the caller may act on it.

    Every content route depends on this, so the tenant boundary is enforced in
    one place rather than repeated in each handler.
    """
    workspace = (
        await session.execute(select(Workspace).where(Workspace.id == workspace_id))
    ).scalar_one_or_none()
    if workspace is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workspace {workspace_id} not found.",
        )
    return workspace


async def require_workspace_admin(
    workspace: Workspace = Depends(require_workspace),
    principal: Principal = Depends(require_admin),
) -> Workspace:
    """A workspace, for a caller allowed to change its contents."""
    return workspace
