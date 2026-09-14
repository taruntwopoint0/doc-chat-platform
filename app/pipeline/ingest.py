"""The ingestion pipeline, stage by stage.

parse -> profile -> chunk -> contextualise -> embed -> ready.

Every stage transition is written to `ingest_jobs` so the UI can poll progress.
The job is resumable: the checkpoint records which stages have completed, and
the embedding stage derives its own remaining work from the database, so a
worker killed mid-document picks up where it stopped instead of starting over.
"""

from __future__ import annotations

import logging
import os
import tempfile
from uuid import UUID

from app.config import Settings, get_settings
from app.interfaces.types import DocumentProfile, DocumentStatus, ParsedDocument
from app.models.document import Document
from app.models.ingest_job import IngestJob
from app.pipeline import persist
from app.pipeline.chunking import ChunkingConfig, chunk_blocks
from app.pipeline.contextualise import contextualise
from app.pipeline.embedding import embed_document
from app.pipeline.profiling import profile_document
from app.registry import get_embedder, get_file_store, get_llm, get_parser_registry
from app.workers import queue

logger = logging.getLogger(__name__)

#: Rough share of a document's work each stage represents, for progress_pct.
_STAGE_PROGRESS = {
    DocumentStatus.PARSING: 10,
    DocumentStatus.PROFILING: 25,
    DocumentStatus.CHUNKING: 40,
    DocumentStatus.EMBEDDING: 50,
}
#: Embedding spans the rest of the bar.
_EMBED_SPAN = 50


class IngestError(RuntimeError):
    pass


async def _advance(job: IngestJob, stage: str, checkpoint: dict | None = None) -> None:
    await queue.update_progress(
        job.id,
        stage=stage,
        progress_pct=_STAGE_PROGRESS.get(stage),
        checkpoint=checkpoint,
    )
    await persist.set_document_status(job.document_id, stage)


async def _materialise(document: Document) -> str:
    """Write the stored bytes to a temp file for the parser to open.

    Parsers take a path because the libraries behind them do. The file lives
    only for the duration of the parse; the durable copy is in the file store.
    """
    file_store = get_file_store()
    key = document.storage_key or str(document.id)
    data = await file_store.get(document.workspace_id, key)
    suffix = os.path.splitext(document.filename)[1] or ""
    handle, path = tempfile.mkstemp(prefix="ingest-", suffix=suffix)
    with os.fdopen(handle, "wb") as fh:
        fh.write(data)
    return path


async def _parse(document: Document) -> ParsedDocument:
    parser = get_parser_registry().get(document.mime_type)
    path = await _materialise(document)
    try:
        parsed = await parser.parse(path, document.mime_type)
    finally:
        try:
            os.unlink(path)
        except OSError:  # pragma: no cover - best effort cleanup
            pass
    if not parsed.blocks:
        raise IngestError(
            f"No text could be extracted from '{document.filename}'. If it is a "
            "scanned document, run the OCR-capable parser (PARSER=docling)."
        )
    return parsed


async def _chunk_and_store(
    document: Document,
    parsed: ParsedDocument,
    profile: DocumentProfile,
    settings: Settings,
) -> int:
    config = ChunkingConfig(
        target_tokens=settings.chunk_target_tokens,
        overlap_ratio=settings.chunk_overlap_ratio,
        max_tokens=settings.embed_max_input_tokens,
    )
    drafts = chunk_blocks(parsed.blocks, config)
    if not drafts:
        raise IngestError(f"'{document.filename}' produced no chunks.")

    await contextualise(
        drafts,
        document_name=document.filename,
        profile=profile,
        llm=get_llm(),
        per_chunk=settings.context_descriptions == "chunk",
    )
    return await persist.replace_chunks(
        document.workspace_id, document.id, drafts, profile
    )


async def run_job(job: IngestJob, embed: bool = True) -> None:
    """Execute one ingestion job. Raises on failure; the worker records it.

    `embed=False` runs every stage except embedding and leaves the document
    short of `ready`, since a document with no vectors is not retrievable. The
    seed script uses it to exercise parsing and chunking without an API key.
    """
    settings = get_settings()
    document = await persist.load_document(job.document_id)
    if document is None:
        raise IngestError(f"Document {job.document_id} no longer exists")

    checkpoint = dict(job.checkpoint or {})
    chunks_written = int(checkpoint.get("chunks_written") or 0)

    # Stages before chunking are skipped on a resumption: their output is
    # already in the database, and re-running them would discard the vectors
    # written so far.
    if chunks_written == 0:
        await _advance(job, DocumentStatus.PARSING, checkpoint)
        parsed = await _parse(document)
        if parsed.page_count is not None:
            await persist.set_document_status(
                document.id, DocumentStatus.PARSING, page_count=parsed.page_count
            )

        await _advance(job, DocumentStatus.PROFILING, checkpoint)
        profile = await profile_document(
            get_llm(),
            parsed.blocks,
            document.filename,
            settings.profile_section_chars,
        )
        await persist.store_document_profile(
            document.workspace_id, document.id, profile
        )

        await _advance(job, DocumentStatus.CHUNKING, checkpoint)
        chunks_written = await _chunk_and_store(document, parsed, profile, settings)
        checkpoint["chunks_written"] = chunks_written
    else:
        logger.info(
            "Resuming job %s: %d chunks already written for %s",
            job.id,
            chunks_written,
            document.filename,
        )

    await _advance(job, DocumentStatus.EMBEDDING, checkpoint)

    if not embed:
        # Stops short of `ready` on purpose: the chunks exist but have no
        # vectors, so the document is not retrievable yet. A later run with
        # embedding enabled finds them via `embedding IS NULL` and finishes.
        logger.info(
            "Embedding skipped for %s; %d chunks left unvectorised",
            document.filename,
            chunks_written,
        )
        return

    async def on_progress(done: int, total: int) -> None:
        span = int(_EMBED_SPAN * done / total)
        pct = _STAGE_PROGRESS[DocumentStatus.EMBEDDING] + span
        await queue.update_progress(
            job.id,
            progress_pct=pct,
            checkpoint={**checkpoint, "embedded": done, "embed_total": total},
        )

    await embed_document(
        document.workspace_id,
        document.id,
        get_embedder(),
        settings.embed_batch_size,
        on_progress,
    )

    await persist.set_document_status(
        document.id, DocumentStatus.READY, mark_indexed=True
    )
    logger.info(
        "Indexed %s (%d chunks) in workspace %s",
        document.filename,
        chunks_written,
        document.workspace_id,
    )


async def requeue_workspace(session, workspace_id: UUID) -> int:
    """Queue every document in a workspace for a fresh ingestion run.

    Used by POST /reindex -- after a chunking or embedding change, the stored
    originals are re-run without anyone re-uploading them.
    """
    from sqlalchemy import select

    from app.models.document import Document as DocumentModel

    documents = (
        await session.execute(
            select(DocumentModel).where(DocumentModel.workspace_id == workspace_id)
        )
    ).scalars().all()

    for document in documents:
        document.status = DocumentStatus.PENDING
        document.error_message = None
        document.indexed_at = None
        # Start from stage one: the checkpoint is empty, so chunks are rebuilt.
        await queue.enqueue(session, workspace_id, document.id)
    return len(documents)
