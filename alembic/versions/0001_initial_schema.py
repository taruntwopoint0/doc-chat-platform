"""Initial schema: workspaces, documents, chunks, ingest jobs.

Revision ID: 0001_initial
Revises:
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql as pg

from app.models.constants import (
    EMBEDDING_DIMENSIONS,
    HNSW_EF_CONSTRUCTION,
    HNSW_M,
    TSV_EXPRESSION,
)

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TS = sa.text("now()")
_EMPTY_OBJ = sa.text("'{}'::jsonb")
_EMPTY_ARR = sa.text("'[]'::jsonb")


def upgrade() -> None:
    # pgvector is the only extension the schema needs; full-text search uses
    # Postgres' built-in tsvector, which requires no extension.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "workspaces",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(255), nullable=False, unique=True),
        sa.Column("config", pg.JSONB, nullable=False, server_default=_EMPTY_OBJ),
        sa.Column("profile", pg.JSONB, nullable=False, server_default=_EMPTY_OBJ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_TS),
    )

    op.create_table(
        "documents",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("filename", sa.String(512), nullable=False),
        sa.Column("mime_type", sa.String(255), nullable=False),
        sa.Column("file_hash", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger, nullable=False),
        sa.Column("version_label", sa.String(64), nullable=False, server_default="v1"),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("page_count", sa.Integer, nullable=True),
        sa.Column("profile", pg.JSONB, nullable=False, server_default=_EMPTY_OBJ),
        sa.Column("storage_key", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_TS),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_documents_workspace_status", "documents", ["workspace_id", "status"])
    op.create_index("ix_documents_workspace_hash", "documents", ["workspace_id", "file_hash"])
    op.create_index(
        "ix_documents_workspace_filename", "documents", ["workspace_id", "filename"]
    )

    op.create_table(
        "document_files",
        sa.Column(
            "document_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "workspace_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("filename", sa.String(512), nullable=False),
        sa.Column("content", sa.LargeBinary, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_TS),
    )

    op.create_table(
        "chunks",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "document_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("contextual_header", sa.Text, nullable=True),
        sa.Column("section_path", pg.JSONB, nullable=False, server_default=_EMPTY_ARR),
        sa.Column("tags", pg.JSONB, nullable=False, server_default=_EMPTY_OBJ),
        sa.Column("token_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("embedding", Vector(EMBEDDING_DIMENSIONS), nullable=True),
        sa.Column(
            "tsv",
            pg.TSVECTOR,
            sa.Computed(TSV_EXPRESSION, persisted=True),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_TS),
        sa.UniqueConstraint("document_id", "ordinal", name="uq_chunks_document_ordinal"),
    )
    op.create_index("ix_chunks_workspace", "chunks", ["workspace_id"])
    op.create_index("ix_chunks_document", "chunks", ["document_id"])
    # Resume cursor for an interrupted embedding run.
    op.create_index(
        "ix_chunks_pending_embedding",
        "chunks",
        ["document_id", "ordinal"],
        postgresql_where=sa.text("embedding IS NULL"),
    )
    op.create_index("ix_chunks_tags", "chunks", ["tags"], postgresql_using="gin")
    # Full-text half of hybrid search (Milestone 2).
    op.create_index("ix_chunks_tsv", "chunks", ["tsv"], postgresql_using="gin")
    # Vector half. HNSW, not IVFFlat: IVFFlat trains its centroids at build
    # time, and this index is built here on an empty table. Untrained, it
    # returns *no rows* rather than merely losing recall -- which this product
    # would surface as "the documents do not cover that", a confident and wrong
    # refusal. HNSW has no training step, so it is correct from the first row
    # and needs no REINDEX as the corpus grows.
    #
    # Build memory: HNSW wants the graph to fit in maintenance_work_mem. On a
    # small instance with a large corpus, raise it for the build
    # (SET maintenance_work_mem = '256MB') or the build spills and slows down.
    op.execute(
        "CREATE INDEX ix_chunks_embedding ON chunks "
        "USING hnsw (embedding vector_cosine_ops) "
        f"WITH (m = {HNSW_M}, ef_construction = {HNSW_EF_CONSTRUCTION})"
    )

    op.create_table(
        "ingest_jobs",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "document_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("stage", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("progress_pct", sa.Integer, nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("checkpoint", pg.JSONB, nullable=False, server_default=_EMPTY_OBJ),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_TS),
    )
    # Access path for the SKIP LOCKED claim query: oldest pending job first.
    op.create_index("ix_ingest_jobs_claim", "ingest_jobs", ["status", "created_at"])
    op.create_index("ix_ingest_jobs_workspace", "ingest_jobs", ["workspace_id"])
    op.create_index("ix_ingest_jobs_document", "ingest_jobs", ["document_id"])


def downgrade() -> None:
    op.drop_table("ingest_jobs")
    op.drop_table("chunks")
    op.drop_table("document_files")
    op.drop_table("documents")
    op.drop_table("workspaces")
