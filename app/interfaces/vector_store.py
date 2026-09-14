"""Vector store boundary."""

from __future__ import annotations

from abc import ABC, abstractmethod
from uuid import UUID

from app.interfaces.types import Chunk, SearchResult


class VectorStore(ABC):
    @abstractmethod
    async def upsert(self, chunks: list[Chunk]) -> None:
        """Insert or replace chunks. Must be idempotent on chunk id."""
        raise NotImplementedError

    @abstractmethod
    async def delete_by_document(self, document_id: UUID) -> None:
        """Remove every chunk belonging to a document."""
        raise NotImplementedError

    @abstractmethod
    async def hybrid_search(
        self,
        workspace_id: UUID,
        query: str,
        query_vector: list[float],
        filters: dict,
        limit: int,
    ) -> list[SearchResult]:
        """Combined vector + full-text search, scoped to one workspace.

        Milestone 2. The signature is fixed now so only the body changes later.
        """
        raise NotImplementedError
