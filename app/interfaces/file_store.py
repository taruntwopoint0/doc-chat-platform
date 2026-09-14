"""Original-file storage boundary.

Render's disk is ephemeral, so the bytes an admin uploaded have to live
somewhere durable for two reasons: a worker restart mid-parse must be able to
resume, and POST /reindex needs to re-read files it never saw uploaded.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from uuid import UUID

from app.interfaces.types import StoredFile


class FileStore(ABC):
    @abstractmethod
    async def put(
        self, workspace_id: UUID, document_id: UUID, data: bytes, filename: str
    ) -> StoredFile:
        """Persist raw bytes and return a handle. Overwrites an existing key."""
        raise NotImplementedError

    @abstractmethod
    async def get(self, workspace_id: UUID, key: str) -> bytes:
        """Read bytes back. Raises FileNotFoundError if the key is gone."""
        raise NotImplementedError

    @abstractmethod
    async def delete(self, workspace_id: UUID, key: str) -> None:
        """Remove a stored file. Succeeds if it is already absent."""
        raise NotImplementedError
