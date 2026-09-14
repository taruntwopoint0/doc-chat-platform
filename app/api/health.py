"""Health check. The one route with no admin token."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app.api.schemas import HealthResponse
from app.config import get_settings
from app.db import session_scope
from app.workers import queue

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health(response: Response) -> HealthResponse:
    """Database connectivity plus the depth of the ingest queue.

    Returns 503 when the database is unreachable so a platform health check
    takes the instance out of rotation rather than serving broken requests.
    """
    try:
        async with session_scope() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:
        logger.warning("Health check failed: %s", exc)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthResponse(
            status="unhealthy",
            database="unreachable",
            detail=f"{type(exc).__name__}: {exc}",
        )

    try:
        pending = await queue.pending_count()
    except Exception as exc:  # pragma: no cover - the SELECT 1 above just passed
        logger.warning("Pending job count failed: %s", exc)
        pending = None

    settings = get_settings()
    return HealthResponse(
        status="ok",
        database="ok",
        pending_jobs=pending,
        parser=settings.parser_impl,
        auth_enabled=settings.auth_enabled,
        max_upload_mb=settings.max_upload_mb,
    )
