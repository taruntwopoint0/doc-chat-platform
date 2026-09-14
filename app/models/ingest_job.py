from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.interfaces.types import DocumentStatus, JobStatus
from app.models.base import Base, TimestampMixin


class IngestJob(Base, TimestampMixin):
    """A unit of background work, queued in Postgres.

    Claimed with SELECT ... FOR UPDATE SKIP LOCKED. Moving to Celery later means
    rewriting app/workers/queue.py only -- nothing else touches this table.
    """

    __tablename__ = "ingest_jobs"
    __table_args__ = (
        # The claim query's access path: oldest pending job first.
        Index("ix_ingest_jobs_claim", "status", "created_at"),
        Index("ix_ingest_jobs_workspace", "workspace_id"),
        Index("ix_ingest_jobs_document", "document_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=JobStatus.PENDING
    )
    #: Which pipeline stage the job is in; mirrors DocumentStatus so the UI can
    #: poll one vocabulary.
    stage: Mapped[str] = mapped_column(
        String(32), nullable=False, default=DocumentStatus.PENDING
    )
    progress_pct: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Stage-local resume state, e.g. {"embedded_chunks": 320}. Lets a restart
    #: continue from the last persisted batch instead of redoing the document.
    checkpoint: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Refreshed while a job runs so a crashed worker's jobs can be reclaimed.
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
