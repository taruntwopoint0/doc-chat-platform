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
    WorkspaceListItem,
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


@router.get("", response_model=list[WorkspaceListItem])
async def list_workspaces(
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_user),
) -> list[WorkspaceListItem]:
    """Every workspace, oldest first, with what its dashboard card shows.

    Available to any signed-in user: you have to be able to see a workspace
    to chat with it, and only uploading and deleting are admin-only.

    Three grouped queries cover every workspace at once, rather than one
    detail request per card.
    """
    workspaces = (
        (await session.execute(select(Workspace).order_by(Workspace.created_at)))
        .scalars()
        .all()
    )

    by_status: dict = {}
    last_seen: dict = {w.id: w.created_at for w in workspaces}
    doc_rows = await session.execute(
        select(
            Document.workspace_id,
            Document.status,
            func.count(),
            func.max(func.greatest(Document.created_at, Document.indexed_at)),
        ).group_by(Document.workspace_id, Document.status)
    )
    for ws_id, doc_status, count, latest in doc_rows:
        by_status.setdefault(ws_id, {})[str(doc_status)] = int(count)
        _bump_latest(last_seen, ws_id, latest)

    chunk_rows = await session.execute(
        select(Chunk.workspace_id, func.count()).group_by(Chunk.workspace_id)
    )
    chunks = {ws_id: int(count) for ws_id, count in chunk_rows}

    # Only this user's messages: history is private, and so is when it happened.
    if principal.user_id is not None:
        chat_rows = await session.execute(
            select(ChatMessage.workspace_id, func.max(ChatMessage.created_at))
            .where(ChatMessage.user_id == principal.user_id)
            .group_by(ChatMessage.workspace_id)
        )
        for ws_id, latest in chat_rows:
            _bump_latest(last_seen, ws_id, latest)

    return [
        WorkspaceListItem(
            id=w.id,
            name=w.name,
            slug=w.slug,
            config=w.config,
            created_at=w.created_at,
            documents=_counts_from(by_status.get(w.id, {}), chunks.get(w.id, 0)),
            top_topics=_most_common((w.profile or {}).get("topics"), 5),
            document_types=_most_common((w.profile or {}).get("document_types"), 3),
            last_activity_at=last_seen[w.id],
        )
        for w in workspaces
    ]


def _bump_latest(latest_by_id: dict, key, when) -> None:
    if when is not None and key in latest_by_id and when > latest_by_id[key]:
        latest_by_id[key] = when


def _most_common(counted: dict | None, limit: int) -> list[str]:
    """Keys of a {value: count} vocabulary, most frequent first."""
    pairs = sorted((counted or {}).items(), key=lambda kv: (-int(kv[1]), kv[0]))
    return [key for key, _ in pairs[:limit]]


def _counts_from(by_status: dict[str, int], chunk_count: int) -> DocumentCounts:
    return DocumentCounts(
        total=sum(by_status.values()),
        ready=by_status.get(DocumentStatus.READY, 0),
        failed=by_status.get(DocumentStatus.FAILED, 0),
        in_progress=sum(n for s, n in by_status.items() if s not in _TERMINAL),
        by_status=by_status,
        chunk_count=chunk_count,
    )


async def _counts(session: AsyncSession, workspace_id) -> DocumentCounts:
    rows = (
        await session.execute(
            select(Document.status, func.count())
            .where(Document.workspace_id == workspace_id)
            .group_by(Document.status)
        )
    ).all()
    chunk_count = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Chunk)
                .where(Chunk.workspace_id == workspace_id)
            )
        ).scalar_one()
    )
    return _counts_from({str(s): int(n) for s, n in rows}, chunk_count)


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
