"""Embedding boundary."""

from __future__ import annotations

from abc import ABC, abstractmethod

# Task types are normalised here so pipeline code never passes a
# provider-specific string. Implementations map these onto their own vocabulary.
TASK_DOCUMENT = "document"
TASK_QUERY = "query"


class Embedder(ABC):
    @abstractmethod
    async def embed_batch(
        self, texts: list[str], task_type: str = TASK_DOCUMENT
    ) -> list[list[float]]:
        """Embed `texts`, returning one vector per input, in order."""
        raise NotImplementedError

    @property
    @abstractmethod
    def dimensions(self) -> int:
        """Vector width. Must match the `embedding` column in the schema."""
        raise NotImplementedError
