from __future__ import annotations

import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import Computed, ForeignKey, Index, Integer, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.models.constants import EMBEDDING_DIMENSIONS, TSV_EXPRESSION


class Chunk(Base, TimestampMixin):
    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "ordinal", name="uq_chunks_document_ordinal"),
        Index("ix_chunks_workspace", "workspace_id"),
        Index("ix_chunks_document", "document_id"),
        # Resume cursor for interrupted embedding runs: "chunks still missing a vector".
        Index(
            "ix_chunks_pending_embedding",
            "document_id",
            "ordinal",
            postgresql_where=text("embedding IS NULL"),
        ),
        Index("ix_chunks_tags", "tags", postgresql_using="gin"),
        Index("ix_chunks_tsv", "tsv", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: Denormalised from documents so every retrieval query can filter on the
    #: tenant boundary without a join.
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)

    #: Clean text for display.
    content: Mapped[str] = mapped_column(Text, nullable=False)
    #: "{document} > {section path} -- {description}", prepended before embedding.
    #: Kept separate so the UI can show `content` alone.
    contextual_header: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Heading breadcrumb, e.g. ["Incident Management", "Escalation"].
    section_path: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    #: Copied from the document profile. Vocabulary is corpus-derived.
    tags: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: NULL until the embedding batch covering this chunk has been persisted.
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(EMBEDDING_DIMENSIONS), nullable=True
    )
    tsv: Mapped[str | None] = mapped_column(
        TSVECTOR, Computed(TSV_EXPRESSION, persisted=True), nullable=True
    )
