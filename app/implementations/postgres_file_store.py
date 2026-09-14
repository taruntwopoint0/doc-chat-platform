"""FileStore backed by a Postgres table.

Render's disk is ephemeral, so uploaded bytes cannot live on it: a restart
mid-parse would strand the job, and POST /reindex would have nothing to re-read.
Bytes go in `document_files`, a table of their own -- `documents` carries only a
storage key, so listing documents never drags file content along with it.

Swapping to object storage means writing an S3FileStore against this same
interface and changing FILE_STORE_IMPL; nothing else moves.
"""

from __future__ import annotations

import hashlib
import logging
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import session_scope
from app.interfaces.file_store import FileStore
from app.interfaces.types import StoredFile
from app.models.document import DocumentFile

logger = logging.getLogger(__name__)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class PostgresFileStore(FileStore):
    """Keys are the document id as a string -- one stored file per document."""

    def __init__(self, session: AsyncSession | None = None) -> None:
        self._session = session

    async def put(
        self, workspace_id: UUID, document_id: UUID, data: bytes, filename: str
    ) -> StoredFile:
        statement = insert(DocumentFile).values(
            document_id=document_id,
            workspace_id=workspace_id,
            filename=filename,
            content=data,
        )
        # Re-uploading the same document replaces its bytes rather than failing.
        statement = statement.on_conflict_do_update(
            index_elements=[DocumentFile.document_id],
            set_={
                "content": statement.excluded.content,
                "filename": statement.excluded.filename,
                "workspace_id": statement.excluded.workspace_id,
            },
        )
        if self._session is not None:
            await self._session.execute(statement)
        else:
            async with session_scope() as session:
                await session.execute(statement)

        return StoredFile(
            key=str(document_id), size_bytes=len(data), sha256=sha256(data)
        )

    async def get(self, workspace_id: UUID, key: str) -> bytes:
        statement = select(DocumentFile.content).where(
            DocumentFile.document_id == UUID(key),
            # Tenant boundary: a key from one workspace cannot read another's.
            DocumentFile.workspace_id == workspace_id,
        )
        if self._session is not None:
            content = (await self._session.execute(statement)).scalar_one_or_none()
        else:
            async with session_scope() as session:
                content = (await session.execute(statement)).scalar_one_or_none()
        if content is None:
            raise FileNotFoundError(
                f"No stored file for document {key} in workspace {workspace_id}"
            )
        return bytes(content)

    async def delete(self, workspace_id: UUID, key: str) -> None:
        statement = delete(DocumentFile).where(
            DocumentFile.document_id == UUID(key),
            DocumentFile.workspace_id == workspace_id,
        )
        if self._session is not None:
            await self._session.execute(statement)
        else:
            async with session_scope() as session:
                await session.execute(statement)
