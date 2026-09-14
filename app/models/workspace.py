from __future__ import annotations

import uuid

from sqlalchemy import String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class Workspace(Base, TimestampMixin):
    """A tenant. Every content row carries this id and every query filters on it."""

    __tablename__ = "workspaces"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)

    #: Operator-supplied knobs (retrieval settings, prompts) -- opaque to the pipeline.
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    #: Accumulated, LLM-derived description of the corpus: topics, entities,
    #: document types. Grows as documents are ingested; never seeded with
    #: domain-specific values.
    profile: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
