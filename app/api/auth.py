"""Sign-in, sign-out and first-run bootstrap."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    COOKIE_NAME,
    LOCAL_USERNAME,
    Principal,
    current_user,
    require_admin,
)
from app.api.schemas import LoginRequest, UserCreate, UserOut
from app.config import Settings, get_settings
from app.db import session_scope
from app.models.account import Session, User
from app.security import (
    hash_password,
    hash_session_token,
    new_session_token,
    verify_password,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

#: Verified against when the username does not exist, so a wrong username
#: and a wrong password take the same time and cannot be told apart.
_DUMMY_HASH = hash_password("not-a-real-password")


def _set_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=settings.session_ttl_hours * 3600,
        httponly=True,  # JavaScript cannot read it, so XSS cannot steal it.
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )


async def create_session(session: AsyncSession, user: User, settings: Settings) -> str:
    token = new_session_token()
    session.add(
        Session(
            token_hash=hash_session_token(token),
            user_id=user.id,
            expires_at=datetime.now(UTC) + timedelta(hours=settings.session_ttl_hours),
            last_seen_at=datetime.now(UTC),
        )
    )
    user.last_login_at = datetime.now(UTC)
    await session.flush()
    return token


@router.post("/login", response_model=UserOut)
async def login(
    payload: LoginRequest,
    response: Response,
    settings: Settings = Depends(get_settings),
) -> UserOut:
    """Exchange a username and password for a session cookie."""
    username = payload.username.strip().lower()
    async with session_scope() as session:
        user = (
            await session.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()

        # Verify even when the user is missing, so a wrong username and a wrong
        # password take the same time and cannot be told apart.
        ok = verify_password(
            payload.password, user.password_hash if user else _DUMMY_HASH
        )

        if not user or not ok or not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Wrong username or password.",
            )
        token = await create_session(session, user, settings)
        out = UserOut.model_validate(user)

    _set_cookie(response, token, settings)
    logger.info("User %s signed in", username)
    return out


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, response: Response) -> None:
    token = request.cookies.get(COOKIE_NAME)
    if token:
        async with session_scope() as session:
            await session.execute(
                delete(Session).where(Session.token_hash == hash_session_token(token))
            )
    response.delete_cookie(COOKIE_NAME, path="/")


@router.get("/me", response_model=UserOut)
async def me(
    request: Request, settings: Settings = Depends(get_settings)
) -> UserOut:
    """Who the browser is signed in as. The UI calls this on load.

    With sign-in off this always answers with the local account, which is what
    makes the dashboard open straight up instead of asking for a password.
    """
    from app.api.deps import local_user

    if settings.auth_enabled:
        user = await current_user(request)
    else:
        user = await local_user()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Not signed in."
        )
    return UserOut.model_validate(user)


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: UserCreate,
    principal: Principal = Depends(require_admin),
) -> UserOut:
    """Add a user. Admins only."""
    username = payload.username.strip().lower()
    async with session_scope() as session:
        exists = (
            await session.execute(select(User.id).where(User.username == username))
        ).scalar_one_or_none()
        if exists:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"User '{username}' already exists.",
            )
        user = User(
            id=uuid4(),
            username=username,
            display_name=payload.display_name or payload.username.strip(),
            password_hash=hash_password(payload.password),
            is_admin=payload.is_admin,
        )
        session.add(user)
        await session.flush()
        return UserOut.model_validate(user)


async def bootstrap_first_user() -> None:
    """Make sure an account exists for however the app is configured.

    With sign-in off, that is the implicit `local` user that chat history hangs
    off. With it on, it is the first real account from the environment, created
    only while the users table is empty so it can never overwrite a password
    someone has since changed.
    """
    settings = get_settings()

    if not settings.auth_enabled:
        async with session_scope() as session:
            exists = (
                await session.execute(
                    select(User.id).where(User.username == LOCAL_USERNAME)
                )
            ).scalar_one_or_none()
            if exists:
                return
            session.add(
                User(
                    id=uuid4(),
                    username=LOCAL_USERNAME,
                    display_name="Local",
                    # Unusable: nothing signs in as this account, and an empty
                    # hash would still be a hash someone could try to match.
                    password_hash=hash_password(new_session_token()),
                    is_admin=True,
                )
            )
        logger.info("Sign-in is off (AUTH_ENABLED=false); using the local account")
        return

    if not settings.admin_username or not settings.admin_password:
        return
    async with session_scope() as session:
        count = (
            await session.execute(select(func.count()).select_from(User))
        ).scalar_one()
        if count:
            return
        session.add(
            User(
                id=uuid4(),
                username=settings.admin_username.strip().lower(),
                display_name=settings.admin_username.strip(),
                password_hash=hash_password(settings.admin_password),
                is_admin=True,
            )
        )
    logger.info(
        "Created the first user '%s' from ADMIN_USERNAME/ADMIN_PASSWORD",
        settings.admin_username,
    )


async def purge_expired_sessions() -> int:
    async with session_scope() as session:
        result = await session.execute(
            delete(Session)
            .where(Session.expires_at < datetime.now(UTC))
            .returning(Session.token_hash)
        )
        return len(result.all())
