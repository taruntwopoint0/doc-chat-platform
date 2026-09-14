"""Embed a document's chunks, in batches, resumably.

Chunks are written to the database before any vector exists, with
`embedding` NULL. This stage fills those in batch by batch, committing after
each one. The set of work left is therefore always derivable from the database
itself -- "chunks of this document where embedding IS NULL" -- so a worker that
dies mid-document resumes from the last committed batch rather than re-embedding
everything, and never needs an in-memory cursor to survive the restart.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from uuid import UUID

from sqlalchemy import bindparam, func, select, update

from app.db import session_scope
from app.interfaces.embedder import TASK_DOCUMENT, Embedder
from app.models.chunk import Chunk

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int], Awaitable[None]]


async def count_pending(workspace_id: UUID, document_id: UUID) -> int:
    async with session_scope() as session:
        result = await session.execute(
            select(func.count())
            .select_from(Chunk)
            .where(
                Chunk.workspace_id == workspace_id,
                Chunk.document_id == document_id,
                Chunk.embedding.is_(None),
            )
        )
        return int(result.scalar_one())


async def _next_batch(
    workspace_id: UUID, document_id: UUID, size: int
) -> list[tuple[UUID, str, str | None]]:
    async with session_scope() as session:
        result = await session.execute(
            select(Chunk.id, Chunk.content, Chunk.contextual_header)
            .where(
                Chunk.workspace_id == workspace_id,
                Chunk.document_id == document_id,
                Chunk.embedding.is_(None),
            )
            .order_by(Chunk.ordinal)
            .limit(size)
        )
        return [(r[0], r[1], r[2]) for r in result.all()]


def embedding_text(content: str, header: str | None) -> str:
    """What the embedder sees. `content` alone is what the UI shows."""
    return f"{header}\n\n{content}" if header else content


async def embed_document(
    workspace_id: UUID,
    document_id: UUID,
    embedder: Embedder,
    batch_size: int,
    on_progress: ProgressCallback | None = None,
) -> int:
    """Embed every chunk of a document that still lacks a vector.

    Returns the number of vectors written by this call, which is less than the
    document's chunk count when the run is a resumption.
    """
    total = await count_pending(workspace_id, document_id)
    if total == 0:
        return 0

    written = 0
    while True:
        batch = await _next_batch(workspace_id, document_id, batch_size)
        if not batch:
            break

        texts = [embedding_text(content, header) for _, content, header in batch]
        vectors = await embedder.embed_batch(texts, TASK_DOCUMENT)
        if len(vectors) != len(batch):
            raise RuntimeError(
                f"Embedder returned {len(vectors)} vectors for {len(batch)} chunks"
            )

        # One statement per batch, committed immediately: the commit is the
        # checkpoint, so there is no window where work is done but unrecorded.
        payload = [
            {"chunk_id": chunk_id, "vector": vector}
            for (chunk_id, _, _), vector in zip(batch, vectors, strict=True)
        ]
        # Against the Core table rather than the ORM entity: the ORM reads an
        # executemany UPDATE as a bulk-update-by-primary-key and rejects the
        # bound WHERE clause. Nothing here needs the ORM -- these rows are
        # written and never read back into the session.
        table = Chunk.__table__
        async with session_scope() as session:
            await session.execute(
                update(table)
                .where(table.c.id == bindparam("chunk_id"))
                .values(embedding=bindparam("vector")),
                payload,
            )

        written += len(batch)
        if on_progress is not None:
            await on_progress(written, total)
        logger.debug(
            "Embedded %d/%d chunks of document %s", written, total, document_id
        )

    return written
