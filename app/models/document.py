from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.interfaces.types import DocumentStatus
from app.models.base import Base, TimestampMixin


class Document(Base, TimestampMixin):
    __tablename__ = "documents"
    __table_args__ = (
        Index("ix_documents_workspace_status", "workspace_id", "status"),
        # Re-upload detection: same bytes in the same workspace is the same doc.
        Index("ix_documents_workspace_hash", "workspace_id", "file_hash"),
        Index("ix_documents_workspace_filename", "workspace_id", "filename"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: Bumped when the same filename arrives with different bytes.
    version_label: Mapped[str] = mapped_column(String(64), nullable=False, default="v1")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=DocumentStatus.PENDING
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Per-document LLM profile; its topics are copied onto each chunk as tags.
    profile: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    #: FileStore handle for the original bytes.
    storage_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    indexed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class DocumentFile(Base, TimestampMixin):
    """Original upload bytes, kept out of `documents` so listing stays cheap.

    Backs PostgresFileStore. Swapping in an S3 FileStore leaves this table unused.
    """

    __tablename__ = "document_files"

    document_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        primary_key=True,
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    content: Mapped[bytes] = mapped_column(nullable=False)
