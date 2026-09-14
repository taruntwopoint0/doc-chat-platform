"""Job resumability and the Postgres-backed queue.

The promise: a worker killed mid-document continues from the last committed
batch. Ingestion is slow and the corpus has no size limit, so restarting a
half-embedded 400-page document from scratch on every deploy is not acceptable.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import func, select, update

from app.interfaces.types import Chunk as ChunkData
from app.interfaces.types import DocumentStatus, JobStatus
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.ingest_job import IngestJob
from app.pipeline.embedding import count_pending, embed_document
from app.workers import queue
from tests.conftest import FakeEmbedder

CHUNK_COUNT = 20
BATCH = 4


async def make_document(session, workspace, status=DocumentStatus.CHUNKING) -> Document:
    document = Document(
        id=uuid4(),
        workspace_id=workspace.id,
        filename="manual.md",
        mime_type="text/markdown",
        file_hash=uuid4().hex,
        size_bytes=1024,
        version_label="v1",
        status=status,
    )
    session.add(document)
    await session.commit()
    return document


async def seed_unembedded(session, workspace, document, count=CHUNK_COUNT):
    from app.implementations.pgvector_store import PgVectorStore

    await PgVectorStore(session).upsert(
        [
            ChunkData(
                id=uuid4(),
                document_id=document.id,
                workspace_id=workspace.id,
                ordinal=i,
                content=f"Chunk number {i} of the manual.",
                contextual_header=f"manual.md > Section {i // 5}",
                section_path=[f"Section {i // 5}"],
                tags={},
                token_count=12,
                embedding=None,
            )
            for i in range(count)
        ]
    )
    await session.commit()


async def embedded_count(session, document_id) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(Chunk)
                .where(Chunk.document_id == document_id, Chunk.embedding.is_not(None))
            )
        ).scalar_one()
    )


# --- embedding resumption ------------------------------------------------


async def test_a_clean_run_embeds_every_chunk(session, workspace):
    document = await make_document(session, workspace)
    await seed_unembedded(session, workspace, document)

    written = await embed_document(
        workspace.id, document.id, FakeEmbedder(), BATCH
    )

    assert written == CHUNK_COUNT
    assert await embedded_count(session, document.id) == CHUNK_COUNT


async def test_a_crash_leaves_completed_batches_persisted(session, workspace):
    document = await make_document(session, workspace)
    await seed_unembedded(session, workspace, document)

    crashing = FakeEmbedder(fail_after=8)
    with pytest.raises(RuntimeError, match="simulated provider failure"):
        await embed_document(workspace.id, document.id, crashing, BATCH)

    # Each batch commits as it completes, so the finished ones survive.
    assert await embedded_count(session, document.id) == 8
    assert await count_pending(workspace.id, document.id) == CHUNK_COUNT - 8


async def test_a_resumed_run_re_embeds_nothing(session, workspace):
    document = await make_document(session, workspace)
    await seed_unembedded(session, workspace, document)

    with pytest.raises(RuntimeError):
        await embed_document(workspace.id, document.id, FakeEmbedder(fail_after=8), BATCH)

    resumed = FakeEmbedder()
    written = await embed_document(workspace.id, document.id, resumed, BATCH)

    assert written == CHUNK_COUNT - 8, "the resumed run redid earlier work"
    assert resumed.embedded == CHUNK_COUNT - 8
    assert await embedded_count(session, document.id) == CHUNK_COUNT

    # The chunks handed to the second embedder must be exactly the tail.
    seen = [text for batch in resumed.batches for text in batch]
    assert len(seen) == CHUNK_COUNT - 8
    assert all("Chunk number" in text for text in seen)
    ordinals = sorted(int(t.split("Chunk number ")[1].split(" ")[0]) for t in seen)
    assert ordinals == list(range(8, CHUNK_COUNT))


async def test_embedding_an_already_complete_document_is_a_no_op(session, workspace):
    document = await make_document(session, workspace)
    await seed_unembedded(session, workspace, document)
    await embed_document(workspace.id, document.id, FakeEmbedder(), BATCH)

    second = FakeEmbedder()
    assert await embed_document(workspace.id, document.id, second, BATCH) == 0
    assert second.batches == []


async def test_the_embedder_sees_the_contextual_header(session, workspace):
    document = await make_document(session, workspace)
    await seed_unembedded(session, workspace, document, count=2)

    embedder = FakeEmbedder()
    await embed_document(workspace.id, document.id, embedder, BATCH)

    first = embedder.batches[0][0]
    assert first.startswith("manual.md > Section 0")
    assert "Chunk number 0" in first


async def test_progress_is_reported_per_batch(session, workspace):
    document = await make_document(session, workspace)
    await seed_unembedded(session, workspace, document)

    seen: list[tuple[int, int]] = []

    async def on_progress(done, total):
        seen.append((done, total))

    await embed_document(
        workspace.id, document.id, FakeEmbedder(), BATCH, on_progress
    )
    assert seen == [(4, 20), (8, 20), (12, 20), (16, 20), (20, 20)]


# --- the job queue -------------------------------------------------------


async def test_claim_marks_the_job_running_and_counts_the_attempt(session, workspace):
    document = await make_document(session, workspace)
    job = await queue.enqueue(session, workspace.id, document.id)
    await session.commit()

    claimed = await queue.claim("worker-1")
    assert claimed is not None and claimed.id == job.id
    assert claimed.status == JobStatus.RUNNING
    assert claimed.attempts == 1
    assert claimed.started_at is not None


async def test_a_claimed_job_is_not_handed_to_a_second_worker(session, workspace):
    document = await make_document(session, workspace)
    await queue.enqueue(session, workspace.id, document.id)
    await session.commit()

    first = await queue.claim("worker-1")
    second = await queue.claim("worker-2")
    assert first is not None
    assert second is None, "the same job was claimed twice"


async def test_workers_take_different_jobs(session, workspace):
    for _ in range(3):
        document = await make_document(session, workspace)
        await queue.enqueue(session, workspace.id, document.id)
    await session.commit()

    claimed = {(await queue.claim(f"w{i}")).id for i in range(3)}
    assert len(claimed) == 3
    assert await queue.claim("w3") is None


async def test_a_released_job_keeps_its_checkpoint(session, workspace):
    document = await make_document(session, workspace)
    job = await queue.enqueue(session, workspace.id, document.id)
    await session.commit()

    await queue.claim("worker-1")
    await queue.update_progress(
        job.id, stage=DocumentStatus.EMBEDDING, checkpoint={"chunks_written": 12}
    )
    await queue.release(job.id, "transient failure", max_attempts=3)

    refreshed = (
        await session.execute(select(IngestJob).where(IngestJob.id == job.id))
    ).scalar_one()
    await session.refresh(refreshed)
    assert refreshed.status == JobStatus.PENDING
    assert refreshed.checkpoint == {"chunks_written": 12}, "resume state was lost"
    assert refreshed.error_message == "transient failure"


async def test_a_job_fails_for_good_once_attempts_run_out(session, workspace):
    document = await make_document(session, workspace)
    job = await queue.enqueue(session, workspace.id, document.id)
    await session.commit()

    for _ in range(3):
        await queue.claim("worker-1")
        await queue.release(job.id, "still failing", max_attempts=3)

    refreshed = (
        await session.execute(select(IngestJob).where(IngestJob.id == job.id))
    ).scalar_one()
    await session.refresh(refreshed)
    assert refreshed.status == JobStatus.FAILED
    assert refreshed.stage == DocumentStatus.FAILED
    assert await queue.claim("worker-1") is None


async def test_a_job_abandoned_by_a_dead_worker_is_reclaimed(session, workspace):
    document = await make_document(session, workspace)
    job = await queue.enqueue(session, workspace.id, document.id)
    await session.commit()
    await queue.claim("worker-1")

    # A worker killed mid-run leaves the row RUNNING with a stale heartbeat.
    await session.execute(
        update(IngestJob).where(IngestJob.id == job.id).values(heartbeat_at=None)
    )
    await session.commit()

    assert await queue.reclaim_stalled(0) == 1
    reclaimed = await queue.claim("worker-2")
    assert reclaimed is not None and reclaimed.id == job.id
    assert reclaimed.attempts == 2, "the reclaim should count as a fresh attempt"


async def test_pending_count_covers_queued_and_running(session, workspace):
    for _ in range(2):
        document = await make_document(session, workspace)
        await queue.enqueue(session, workspace.id, document.id)
    await session.commit()

    assert await queue.pending_count() == 2
    await queue.claim("worker-1")
    assert await queue.pending_count() == 2, "a running job is still outstanding"


# --- stage skipping on resume -------------------------------------------


async def test_a_resumed_job_skips_completed_stages(
    session, workspace, monkeypatch
):
    """The expensive stages are skipped when chunks are already written."""
    from app.pipeline import ingest

    document = await make_document(session, workspace)
    await seed_unembedded(session, workspace, document)
    job = await queue.enqueue(session, workspace.id, document.id)
    job.checkpoint = {"chunks_written": CHUNK_COUNT}
    await session.commit()
    await session.refresh(job)

    async def explode(*args, **kwargs):
        raise AssertionError("a resumed job re-ran a completed stage")

    monkeypatch.setattr(ingest, "_parse", explode)
    monkeypatch.setattr(ingest, "profile_document", explode)
    monkeypatch.setattr(ingest, "_chunk_and_store", explode)
    monkeypatch.setattr(ingest, "get_embedder", lambda: FakeEmbedder())

    await ingest.run_job(job)

    await session.refresh(document)
    assert document.status == DocumentStatus.READY
    assert document.indexed_at is not None
    assert await embedded_count(session, document.id) == CHUNK_COUNT
