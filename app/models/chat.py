from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class ChatMessage(Base, TimestampMixin):
    """One turn of a conversation, kept until the user clears it.

    Scoped to (workspace, user): documents are a shared corpus, but what
    someone asked about them is theirs. Switching workspace therefore switches
    conversation, which is what makes "compliance" and "onboarding" feel like
    separate places rather than one transcript.
    """

    __tablename__ = "chat_messages"
    __table_args__ = (
        # The history query: this user's messages in this workspace, in order.
        Index("ix_chat_workspace_user", "workspace_id", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: "user" or "assistant".
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    #: Assistant turns carry the citations that backed the answer, so reloading
    #: history shows the same sources the answer was built from.
    citations: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    #: True when the assistant declined because the corpus did not cover it.
    refused: Mapped[bool] = mapped_column(nullable=False, default=False)
