"""Accept an upload: validate, deduplicate, version, queue.

Runs inside the HTTP request and must stay cheap -- it hashes the bytes, writes
two rows and returns. Parsing never happens here.

Three outcomes, decided by the SHA-256 of the file:

* same hash, already indexed -> nothing to do, the existing document is returned;
* same filename, different hash -> a new version of that document: its chunks
  are deleted and the row is re-queued, so stale text cannot outlive a re-upload;
* otherwise -> a new document.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.implementations.parsers import mime as mimes
from app.implementations.postgres_file_store import sha256
from app.interfaces.types import DocumentStatus
from app.models.document import Document
from app.models.ingest_job import IngestJob
from app.registry import get_file_store, get_parser_registry, get_vector_store
from app.workers.queue import enqueue

logger = logging.getLogger(__name__)


class UploadTooLarge(ValueError):
    def __init__(self, size_bytes: int, limit_bytes: int) -> None:
        super().__init__(
            f"File is {size_bytes / 1_048_576:.1f} MB; the limit is "
            f"{limit_bytes / 1_048_576:.0f} MB."
        )


@dataclass(slots=True)
class IntakeResult:
    document: Document
    job: IngestJob | None
    #: True when the identical file was already indexed and was not re-queued.
    deduplicated: bool
    #: True when this replaced an earlier version of the same filename.
    superseded: bool


def _next_version(current: str | None) -> str:
    if current and current.startswith("v") and current[1:].isdigit():
        return f"v{int(current[1:]) + 1}"
    return "v2"


async def intake_upload(
    session: AsyncSession,
    workspace_id: UUID,
    filename: str,
    data: bytes,
    declared_mime: str | None,
    max_upload_bytes: int,
) -> IntakeResult:
    if len(data) > max_upload_bytes:
        raise UploadTooLarge(len(data), max_upload_bytes)
    if not data:
        raise ValueError(f"'{filename}' is empty.")

    # Raises UnsupportedFileType, which the route turns into a 415 listing the
    # accepted types.
    mime_type = mimes.detect_mime(data, filename, declared_mime)
    if not get_parser_registry().supports(mime_type):
        raise mimes.UnsupportedFileType(mime_type, filename)

    file_hash = sha256(data)

    identical = (
        await session.execute(
            select(Document).where(
                Document.workspace_id == workspace_id,
                Document.file_hash == file_hash,
            )
        )
    ).scalar_one_or_none()

    if identical is not None and identical.status == DocumentStatus.READY:
        logger.info(
            "Skipping re-index of %s: identical content already indexed as %s",
            filename,
            identical.id,
        )
        return IntakeResult(identical, None, deduplicated=True, superseded=False)

    existing = identical or (
        await session.execute(
            select(Document).where(
                Document.workspace_id == workspace_id,
                Document.filename == filename,
            )
        )
    ).scalar_one_or_none()

    superseded = False
    if existing is not None:
        if existing.file_hash != file_hash:
            existing.version_label = _next_version(existing.version_label)
            superseded = True
        # Chunks of the previous version must not survive, whether this is a
        # new version or a retry of a failed run.
        await get_vector_store(session).delete_by_document(existing.id, workspace_id)
        existing.file_hash = file_hash
        existing.mime_type = mime_type
        existing.size_bytes = len(data)
        existing.filename = filename
        existing.status = DocumentStatus.PENDING
        existing.error_message = None
        existing.page_count = None
        existing.indexed_at = None
        document = existing
    else:
        document = Document(
            id=uuid4(),
            workspace_id=workspace_id,
            filename=filename,
            mime_type=mime_type,
            file_hash=file_hash,
            size_bytes=len(data),
            version_label="v1",
            status=DocumentStatus.PENDING,
        )
        session.add(document)
        await session.flush()

    stored = await get_file_store(session).put(
        workspace_id, document.id, data, filename
    )
    document.storage_key = stored.key

    job = await enqueue(session, workspace_id, document.id)
    return IntakeResult(document, job, deduplicated=False, superseded=superseded)
