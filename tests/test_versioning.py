"""Hash-based deduplication and version replacement.

The rule that matters: stale chunks must never survive a re-upload. A reader
asking about a procedure that was revised last week must not be answered from
the version it replaced.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.implementations.postgres_file_store import PostgresFileStore, sha256
from app.interfaces.types import Chunk as ChunkData
from app.interfaces.types import DocumentStatus
from app.models.chunk import Chunk
from app.models.document import Document, DocumentFile
from app.models.ingest_job import IngestJob
from app.pipeline.intake import UploadTooLarge, intake_upload

V1 = b"# Escalation\n\nEscalate a P1 within 15 minutes.\n"
V2 = b"# Escalation\n\nEscalate a P1 within 5 minutes. This is the revised rule.\n"


async def upload(session, workspace, data, filename="runbook.md", limit=50 * 1024 * 1024):
    return await intake_upload(
        session,
        workspace_id=workspace.id,
        filename=filename,
        data=data,
        declared_mime="text/markdown",
        max_upload_bytes=limit,
    )


async def seed_chunks(session, workspace_id, document_id, texts):
    """Pretend an ingestion run already produced chunks for this document."""
    from app.implementations.pgvector_store import PgVectorStore

    await PgVectorStore(session).upsert(
        [
            ChunkData(
                id=uuid4(),
                document_id=document_id,
                workspace_id=workspace_id,
                ordinal=index,
                content=text,
                contextual_header=f"runbook.md - {text[:20]}",
                section_path=["Escalation"],
                tags={"topics": ["escalation"]},
                token_count=10,
                embedding=[0.1] * 768,
            )
            for index, text in enumerate(texts)
        ]
    )
    await session.commit()


async def chunk_contents(session, document_id) -> list[str]:
    rows = await session.execute(
        select(Chunk.content)
        .where(Chunk.document_id == document_id)
        .order_by(Chunk.ordinal)
    )
    return [r[0] for r in rows.all()]


# --- first upload --------------------------------------------------------


async def test_first_upload_creates_a_document_and_a_job(session, workspace):
    result = await upload(session, workspace, V1)
    await session.commit()

    assert result.deduplicated is False
    assert result.superseded is False
    assert result.document.version_label == "v1"
    assert result.document.status == DocumentStatus.PENDING
    assert result.document.file_hash == sha256(V1)
    assert result.job is not None

    job = (
        await session.execute(select(IngestJob).where(IngestJob.id == result.job.id))
    ).scalar_one()
    assert job.document_id == result.document.id
    assert job.status == "pending"


async def test_the_original_bytes_are_stored_and_readable(session, workspace):
    result = await upload(session, workspace, V1)
    await session.commit()

    store = PostgresFileStore(session)
    assert await store.get(workspace.id, result.document.storage_key) == V1


async def test_stored_bytes_live_outside_the_documents_table(session, workspace):
    """Listing documents must never drag file content along with it."""
    result = await upload(session, workspace, V1)
    await session.commit()

    assert "content" not in {c.name for c in Document.__table__.columns}
    stored = (
        await session.execute(
            select(DocumentFile).where(DocumentFile.document_id == result.document.id)
        )
    ).scalar_one()
    assert bytes(stored.content) == V1


# --- identical re-upload -------------------------------------------------


async def test_identical_content_is_not_reindexed(session, workspace):
    first = await upload(session, workspace, V1)
    first.document.status = DocumentStatus.READY
    await session.commit()

    second = await upload(session, workspace, V1)
    await session.commit()

    assert second.deduplicated is True
    assert second.job is None
    assert second.document.id == first.document.id
    assert second.document.version_label == "v1"

    jobs = (
        await session.execute(
            select(func.count()).select_from(IngestJob).where(
                IngestJob.document_id == first.document.id
            )
        )
    ).scalar_one()
    assert jobs == 1, "a duplicate upload queued a second job"


async def test_identical_content_after_a_failure_is_requeued(session, workspace):
    """Deduplication applies to indexed documents, not to failed ones."""
    first = await upload(session, workspace, V1)
    first.document.status = DocumentStatus.FAILED
    first.document.error_message = "boom"
    await session.commit()

    second = await upload(session, workspace, V1)
    await session.commit()

    assert second.deduplicated is False
    assert second.job is not None
    assert second.document.status == DocumentStatus.PENDING
    assert second.document.error_message is None


# --- new version ---------------------------------------------------------


async def test_same_filename_different_hash_becomes_a_new_version(session, workspace):
    first = await upload(session, workspace, V1)
    first.document.status = DocumentStatus.READY
    await session.commit()

    second = await upload(session, workspace, V2)
    await session.commit()

    assert second.superseded is True
    assert second.document.id == first.document.id, "a version reuses the document row"
    assert second.document.version_label == "v2"
    assert second.document.file_hash == sha256(V2)
    assert second.document.status == DocumentStatus.PENDING
    assert second.document.indexed_at is None


async def test_stale_chunks_never_survive_a_new_version(session, workspace):
    first = await upload(session, workspace, V1)
    first.document.status = DocumentStatus.READY
    await session.commit()
    await seed_chunks(
        session,
        workspace.id,
        first.document.id,
        ["Escalate a P1 within 15 minutes.", "Old appendix text."],
    )
    assert len(await chunk_contents(session, first.document.id)) == 2

    await upload(session, workspace, V2)
    await session.commit()

    remaining = await chunk_contents(session, first.document.id)
    assert remaining == [], "chunks from the replaced version were left behind"


async def test_version_label_climbs_with_each_revision(session, workspace):
    await upload(session, workspace, V1)
    await session.commit()
    for expected, body in [("v2", V2), ("v3", V1 + b"\nthird"), ("v4", V2 + b"\nfourth")]:
        result = await upload(session, workspace, body)
        await session.commit()
        assert result.document.version_label == expected


async def test_a_new_version_replaces_the_stored_original(session, workspace):
    first = await upload(session, workspace, V1)
    await session.commit()
    await upload(session, workspace, V2)
    await session.commit()

    store = PostgresFileStore(session)
    assert await store.get(workspace.id, first.document.storage_key) == V2


async def test_a_different_filename_is_a_separate_document(session, workspace):
    first = await upload(session, workspace, V1, filename="runbook.md")
    await session.commit()
    second = await upload(session, workspace, V2, filename="other.md")
    await session.commit()

    assert second.document.id != first.document.id
    assert second.document.version_label == "v1"
    assert second.superseded is False


# --- tenancy and validation ---------------------------------------------


async def test_the_same_file_in_two_workspaces_stays_separate(session, workspace):
    from app.models.workspace import Workspace

    other = Workspace(id=uuid4(), name="Other", slug=f"o-{uuid4().hex[:8]}",
                      config={}, profile={})
    session.add(other)
    await session.commit()

    a = await upload(session, workspace, V1)
    await session.commit()
    b = await upload(session, other, V1)
    await session.commit()

    assert a.document.id != b.document.id
    assert b.deduplicated is False, "dedup must not reach across the tenant boundary"


async def test_a_file_store_key_cannot_be_read_from_another_workspace(session, workspace):
    from app.models.workspace import Workspace

    other = Workspace(id=uuid4(), name="Other", slug=f"o-{uuid4().hex[:8]}",
                      config={}, profile={})
    session.add(other)
    await session.commit()

    result = await upload(session, workspace, V1)
    await session.commit()

    with pytest.raises(FileNotFoundError):
        await PostgresFileStore(session).get(other.id, result.document.storage_key)


async def test_oversized_uploads_are_rejected(session, workspace):
    with pytest.raises(UploadTooLarge):
        await upload(session, workspace, V1 * 100, limit=16)


async def test_empty_uploads_are_rejected(session, workspace):
    with pytest.raises(ValueError, match="empty"):
        await upload(session, workspace, b"")


async def test_unsupported_types_are_rejected(session, workspace):
    from app.implementations.parsers.mime import UnsupportedFileType

    with pytest.raises(UnsupportedFileType):
        await intake_upload(
            session,
            workspace_id=workspace.id,
            filename="archive.tar",
            data=b"\x00\x01\x02\x03\xff\xfe\x00\x00",
            declared_mime="application/x-tar",
            max_upload_bytes=1024,
        )
