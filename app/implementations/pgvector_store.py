"""Postgres + pgvector implementation of VectorStore.

Vectors, full-text and metadata all live in one table, so a hybrid query is a
single statement with no cross-store join and no second system to keep in sync.
"""

from __future__ import annotations

import json
import logging
from uuid import UUID

from sqlalchemy import delete, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import session_scope
from app.interfaces.types import Chunk as ChunkData
from app.interfaces.types import SearchResult
from app.interfaces.vector_store import VectorStore
from app.models.chunk import Chunk

logger = logging.getLogger(__name__)

#: Reciprocal rank fusion constant. 60 is the value from the original RRF paper
#: and is the de facto default; it damps the influence of the top few ranks so
#: one half cannot dominate the other.
_RRF_K = 60

#: Two candidate pools, fused on rank. A FULL OUTER JOIN so a chunk found by
#: only one half still scores -- which is the point of running both: vectors
#: catch paraphrase, full text catches exact identifiers like "P1" or "C-042".
_HYBRID_SQL = """
WITH vec AS (
    SELECT ch.id,
           ROW_NUMBER() OVER (ORDER BY ch.embedding <=> CAST(:qv AS vector)) AS rank
    FROM chunks ch
    JOIN documents d ON d.id = ch.document_id
    WHERE ch.workspace_id = CAST(:ws AS uuid)
      AND d.status = 'ready'
      AND ch.embedding IS NOT NULL
      {tag_clause}
    ORDER BY ch.embedding <=> CAST(:qv AS vector)
    LIMIT :pool
),
fts AS (
    SELECT ch.id,
           ROW_NUMBER() OVER (
               ORDER BY ts_rank_cd(ch.tsv, websearch_to_tsquery(:lang, :q)) DESC
           ) AS rank
    FROM chunks ch
    JOIN documents d ON d.id = ch.document_id
    WHERE ch.workspace_id = CAST(:ws AS uuid)
      AND d.status = 'ready'
      AND ch.tsv @@ websearch_to_tsquery(:lang, :q)
      {tag_clause}
    ORDER BY ts_rank_cd(ch.tsv, websearch_to_tsquery(:lang, :q)) DESC
    LIMIT :pool
),
fused AS (
    SELECT COALESCE(vec.id, fts.id) AS id,
           COALESCE(1.0 / (:k + vec.rank), 0.0)
         + COALESCE(1.0 / (:k + fts.rank), 0.0) AS score,
           vec.rank AS vector_rank,
           fts.rank AS text_rank
    FROM vec
    FULL OUTER JOIN fts ON fts.id = vec.id
)
SELECT ch.id, ch.document_id, ch.content, ch.contextual_header,
       ch.section_path, ch.tags, ch.ordinal,
       d.filename, fused.score, fused.vector_rank, fused.text_rank
FROM fused
JOIN chunks ch ON ch.id = fused.id
JOIN documents d ON d.id = ch.document_id
ORDER BY fused.score DESC, ch.ordinal
LIMIT :limit
"""


def _row(chunk: ChunkData) -> dict:
    return {
        "id": chunk.id,
        "document_id": chunk.document_id,
        "workspace_id": chunk.workspace_id,
        "ordinal": chunk.ordinal,
        "content": chunk.content,
        "contextual_header": chunk.contextual_header,
        "section_path": list(chunk.section_path),
        "tags": dict(chunk.tags),
        "token_count": chunk.token_count,
        "embedding": chunk.embedding,
    }


class PgVectorStore(VectorStore):
    def __init__(self, session: AsyncSession | None = None) -> None:
        # A caller inside a transaction passes its session so the write joins
        # that transaction; the background worker lets the store open its own.
        self._session = session

    async def _run(self, statement) -> None:
        if self._session is not None:
            await self._session.execute(statement)
            return
        async with session_scope() as session:
            await session.execute(statement)

    async def upsert(self, chunks: list[ChunkData]) -> None:
        """Insert or replace by chunk id.

        Idempotent on purpose: a worker that dies after writing a batch but
        before recording progress re-runs that batch on restart.
        """
        if not chunks:
            return
        rows = [_row(c) for c in chunks]
        statement = insert(Chunk).values(rows)
        statement = statement.on_conflict_do_update(
            index_elements=[Chunk.id],
            set_={
                "content": statement.excluded.content,
                "contextual_header": statement.excluded.contextual_header,
                "section_path": statement.excluded.section_path,
                "tags": statement.excluded.tags,
                "token_count": statement.excluded.token_count,
                "embedding": statement.excluded.embedding,
            },
        )
        await self._run(statement)

    async def delete_by_document(
        self, document_id: UUID, workspace_id: UUID | None = None
    ) -> None:
        """Remove every chunk of a document.

        `workspace_id` is optional to keep the interface signature, but every
        caller in the pipeline passes it: scoping the delete means a bug in id
        handling cannot reach across the tenant boundary.
        """
        statement = delete(Chunk).where(Chunk.document_id == document_id)
        if workspace_id is not None:
            statement = statement.where(Chunk.workspace_id == workspace_id)
        await self._run(statement)

    async def hybrid_search(
        self,
        workspace_id: UUID,
        query: str,
        query_vector: list[float],
        filters: dict,
        limit: int,
    ) -> list[SearchResult]:
        """Vector + full-text search fused by reciprocal rank.

        RRF rather than a weighted score sum: the two halves produce
        incomparable numbers (cosine distance vs ts_rank_cd), so blending them
        directly needs a magic weight that has to be retuned per corpus. RRF
        only uses each result's *position*, which needs no tuning and is robust
        when one half returns nothing.

        Both halves filter on workspace_id -- the tenant boundary -- and on
        documents being `ready`, so half-embedded documents never leak into
        answers.
        """
        if not query.strip() and query_vector is None:
            return []

        # Each half contributes its own candidate pool; fusion then re-ranks.
        pool = max(limit * 4, 20)
        params: dict = {
            "ws": str(workspace_id),
            "q": query,
            "qv": str(query_vector),
            "pool": pool,
            "limit": limit,
            "lang": get_settings().fts_language,
            "k": _RRF_K,
        }

        tag_clause = ""
        for index, (key, value) in enumerate((filters or {}).items()):
            # Containment on the jsonb tags column, so it can use ix_chunks_tags.
            name = f"tag{index}"
            tag_clause += f" AND ch.tags @> CAST(:{name} AS jsonb)"
            params[name] = json.dumps({key: value})

        sql = _HYBRID_SQL.format(tag_clause=tag_clause)

        if self._session is not None:
            rows = (await self._session.execute(text(sql), params)).mappings().all()
        else:
            async with session_scope() as session:
                rows = (await session.execute(text(sql), params)).mappings().all()

        return [
            SearchResult(
                chunk_id=row["id"],
                document_id=row["document_id"],
                content=row["content"],
                contextual_header=row["contextual_header"],
                section_path=list(row["section_path"] or []),
                score=float(row["score"]),
                metadata={
                    "filename": row["filename"],
                    "ordinal": row["ordinal"],
                    "tags": row["tags"],
                    "vector_rank": row["vector_rank"],
                    "text_rank": row["text_rank"],
                },
            )
            for row in rows
        ]
