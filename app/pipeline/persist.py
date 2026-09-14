"""Database writes shared by the ingestion stages.

Kept apart from the orchestrator so ingest.py reads as a sequence of stages
rather than a sequence of SQL statements.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import func, select, update

from app.db import session_scope
from app.interfaces.types import Chunk as ChunkData
from app.interfaces.types import ChunkDraft, DocumentProfile
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.workspace import Workspace
from app.pipeline.profiling import chunk_tags, merge_workspace_profile
from app.registry import get_vector_store

logger = logging.getLogger(__name__)


async def set_document_status(
    document_id: UUID,
    status: str,
    error_message: str | None = None,
    page_count: int | None = None,
    mark_indexed: bool = False,
) -> None:
    values: dict = {"status": status, "error_message": error_message}
    if page_count is not None:
        values["page_count"] = page_count
    if mark_indexed:
        values["indexed_at"] = datetime.now(UTC)
    async with session_scope() as session:
        await session.execute(
            update(Document).where(Document.id == document_id).values(**values)
        )


async def store_document_profile(
    workspace_id: UUID, document_id: UUID, profile: DocumentProfile
) -> None:
    """Save the document profile and fold it into the workspace vocabulary.

    Both writes share one transaction so the workspace can never count a
    document whose own profile failed to save.
    """
    async with session_scope() as session:
        await session.execute(
            update(Document)
            .where(Document.id == document_id)
            .values(profile=profile.to_dict())
        )
        workspace = (
            await session.execute(
                select(Workspace)
                .where(Workspace.id == workspace_id)
                .with_for_update()
            )
        ).scalar_one()
        workspace.profile = merge_workspace_profile(workspace.profile, profile)


async def replace_chunks(
    workspace_id: UUID,
    document_id: UUID,
    drafts: list[ChunkDraft],
    profile: DocumentProfile,
) -> int:
    """Write chunk rows with no vectors yet, replacing whatever was there.

    Vectors are filled in by the embedding stage. Writing the text first is
    what makes that stage resumable: the work outstanding is always visible in
    the table as `embedding IS NULL`.
    """
    tags = chunk_tags(profile)
    chunks = [
        ChunkData(
            id=uuid4(),
            document_id=document_id,
            workspace_id=workspace_id,
            ordinal=draft.ordinal,
            content=draft.content,
            contextual_header=draft.contextual_header,
            section_path=draft.section_path,
            tags=tags,
            token_count=draft.token_count,
            embedding=None,
        )
        for draft in drafts
    ]

    async with session_scope() as session:
        store = get_vector_store(session)
        # Delete first: a re-run must not leave chunks from the previous
        # version behind, and ordinals would otherwise collide.
        await store.delete_by_document(document_id, workspace_id)
        await store.upsert(chunks)
    return len(chunks)


async def count_chunks(workspace_id: UUID, document_id: UUID) -> int:
    async with session_scope() as session:
        result = await session.execute(
            select(func.count())
            .select_from(Chunk)
            .where(
                Chunk.workspace_id == workspace_id,
                Chunk.document_id == document_id,
            )
        )
        return int(result.scalar_one())


async def load_document(document_id: UUID) -> Document | None:
    async with session_scope() as session:
        return (
            await session.execute(select(Document).where(Document.id == document_id))
        ).scalar_one_or_none()
