"""Workspace routes."""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    Principal,
    get_session,
    require_admin,
    require_user,
    require_workspace,
    require_workspace_admin,
)
from app.api.schemas import (
    DocumentCounts,
    ReindexAccepted,
    WorkspaceCreate,
    WorkspaceDeleted,
    WorkspaceDetail,
    WorkspaceSummary,
)
from app.interfaces.types import DocumentStatus
from app.models.chat import ChatMessage
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.workspace import Workspace
from app.pipeline.ingest import requeue_workspace

router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])

_TERMINAL = {DocumentStatus.READY, DocumentStatus.FAILED}


@router.post("", response_model=WorkspaceSummary, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    payload: WorkspaceCreate,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> Workspace:
    workspace = Workspace(
        id=uuid4(),
        name=payload.name,
        slug=payload.slug,
        config=payload.config,
        # Starts empty. It fills with whatever vocabulary the corpus turns out
        # to use -- nothing is seeded, so no domain is baked in.
        profile={},
    )
    session.add(workspace)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A workspace with slug '{payload.slug}' already exists.",
        ) from exc
    await session.commit()
    return workspace


@router.get("", response_model=list[WorkspaceSummary])
async def list_workspaces(
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_user),
) -> list[Workspace]:
    """Every workspace, newest last. The dashboard's workspace picker.

    Available to any signed-in user: you have to be able to see a workspace
    to chat with it, and only uploading and deleting are admin-only.
    """
    rows = await session.execute(select(Workspace).order_by(Workspace.created_at))
    return list(rows.scalars().all())


async def _counts(session: AsyncSession, workspace_id) -> DocumentCounts:
    rows = (
        await session.execute(
            select(Document.status, func.count())
            .where(Document.workspace_id == workspace_id)
            .group_by(Document.status)
        )
    ).all()
    by_status = {str(s): int(n) for s, n in rows}

    chunk_count = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Chunk)
                .where(Chunk.workspace_id == workspace_id)
            )
        ).scalar_one()
    )

    return DocumentCounts(
        total=sum(by_status.values()),
        ready=by_status.get(DocumentStatus.READY, 0),
        failed=by_status.get(DocumentStatus.FAILED, 0),
        in_progress=sum(n for s, n in by_status.items() if s not in _TERMINAL),
        by_status=by_status,
        chunk_count=chunk_count,
    )


@router.get("/{workspace_id}", response_model=WorkspaceDetail)
async def get_workspace(
    workspace: Workspace = Depends(require_workspace),
    session: AsyncSession = Depends(get_session),
) -> WorkspaceDetail:
    return WorkspaceDetail(
        id=workspace.id,
        name=workspace.name,
        slug=workspace.slug,
        config=workspace.config,
        profile=workspace.profile,
        created_at=workspace.created_at,
        documents=await _counts(session, workspace.id),
    )


@router.delete("/{workspace_id}", response_model=WorkspaceDeleted)
async def delete_workspace(
    workspace: Workspace = Depends(require_workspace_admin),
    session: AsyncSession = Depends(get_session),
) -> WorkspaceDeleted:
    """Delete a workspace and everything in it.

    Documents, their stored originals, every chunk and vector, queued jobs and
    the conversation all go, via ON DELETE CASCADE. There is no undo, so the
    counts are gathered first and returned -- the UI shows them in its
    confirmation so nobody deletes a corpus they had forgotten the size of.

    A job running for this workspace at the time fails on its next step with
    "Document ... no longer exists", which the worker records normally.
    """
    counts = await _counts(session, workspace.id)
    messages = int(
        (
            await session.execute(
                select(func.count())
                .select_from(ChatMessage)
                .where(ChatMessage.workspace_id == workspace.id)
            )
        ).scalar_one()
    )
    name = workspace.name

    await session.delete(workspace)
    await session.commit()

    return WorkspaceDeleted(
        workspace_id=workspace.id,
        name=name,
        documents_deleted=counts.total,
        chunks_deleted=counts.chunk_count,
        messages_deleted=messages,
        message=(
            f"Deleted “{name}” with {counts.total} document(s), "
            f"{counts.chunk_count} chunk(s) and {messages} chat message(s)."
        ),
    )


@router.post(
    "/{workspace_id}/reindex",
    response_model=ReindexAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def reindex_workspace(
    workspace: Workspace = Depends(require_workspace_admin),
    session: AsyncSession = Depends(get_session),
) -> ReindexAccepted:
    """Re-run ingestion for every document, from the stored originals.

    Returns immediately; the worker drains the queue. Existing chunks stay
    searchable until each document's own run replaces them.
    """
    queued = await requeue_workspace(session, workspace.id)
    await session.commit()
    return ReindexAccepted(
        workspace_id=workspace.id,
        documents_queued=queued,
        message=f"Queued {queued} document(s) for reindexing.",
    )
