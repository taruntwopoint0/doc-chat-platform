"""Request and response models."""

from __future__ import annotations

import re
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=1, max_length=255)
    config: dict = Field(default_factory=dict)

    @field_validator("slug")
    @classmethod
    def _check_slug(cls, value: str) -> str:
        value = value.strip().lower()
        if not _SLUG.match(value):
            raise ValueError(
                "slug must be lowercase alphanumeric words separated by hyphens"
            )
        return value


class WorkspaceSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    slug: str
    config: dict
    created_at: datetime


class DocumentCounts(BaseModel):
    """Documents by status, plus the totals a dashboard needs."""

    total: int = 0
    ready: int = 0
    failed: int = 0
    in_progress: int = 0
    by_status: dict[str, int] = Field(default_factory=dict)
    chunk_count: int = 0


class WorkspaceDetail(WorkspaceSummary):
    #: Accumulated, corpus-derived vocabulary. Empty until documents are indexed.
    profile: dict
    documents: DocumentCounts


class WorkspaceListItem(WorkspaceSummary):
    """One card on the dashboard's workspace gallery, complete without a
    second request per workspace."""

    documents: DocumentCounts
    #: The most frequent corpus-derived topics and document types, most common first.
    top_topics: list[str] = Field(default_factory=list)
    document_types: list[str] = Field(default_factory=list)
    #: Latest of: created, a document added or indexed, this user's last message.
    last_activity_at: datetime


class DocumentSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    workspace_id: UUID
    filename: str
    mime_type: str
    file_hash: str
    size_bytes: int
    version_label: str
    status: str
    error_message: str | None
    page_count: int | None
    created_at: datetime
    indexed_at: datetime | None


class DocumentListItem(DocumentSummary):
    chunk_count: int = 0


class UploadAccepted(BaseModel):
    document_id: UUID
    job_id: UUID | None
    status: str
    version_label: str
    #: True when the identical file was already indexed and no work was queued.
    deduplicated: bool = False
    #: True when this replaced an earlier version of the same filename.
    superseded: bool = False
    message: str


class JobStatusResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    workspace_id: UUID
    document_id: UUID
    status: str
    stage: str
    progress_pct: int
    error_message: str | None
    attempts: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    checkpoint: dict


class ReindexAccepted(BaseModel):
    workspace_id: UUID
    documents_queued: int
    message: str


class WorkspaceDeleted(BaseModel):
    workspace_id: UUID
    name: str
    documents_deleted: int
    chunks_deleted: int
    messages_deleted: int
    message: str


class DeleteResult(BaseModel):
    document_id: UUID
    chunks_deleted: int
    message: str


class HealthResponse(BaseModel):
    status: str
    database: str
    pending_jobs: int | None = None
    parser: str | None = None
    #: Lets the dashboard hide the sign-out control when nobody signs in.
    auth_enabled: bool = False
    #: The dashboard renders its own limit from this, so the text it shows
    #: can never disagree with what the server actually accepts.
    max_upload_mb: int | None = None
    detail: str | None = None


# --- Chat and search (Milestone 2) ----------------------------------------


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    #: Metadata narrowing, e.g. {"document_type": "runbook"}. Corpus-derived.
    filters: dict = Field(default_factory=dict)
    top_k: int | None = Field(default=None, ge=1, le=50)


class CitationOut(BaseModel):
    number: int
    document_id: UUID
    chunk_id: UUID
    filename: str
    section_path: list[str]
    snippet: str
    score: float


class ChatResponse(BaseModel):
    question: str
    answer: str
    #: True when the corpus did not support an answer. The UI styles these
    #: differently so a refusal is never mistaken for a finding.
    refused: bool
    #: How many passages were retrieved and offered to the model.
    considered: int
    citations: list[CitationOut] = Field(default_factory=list)


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    filters: dict = Field(default_factory=dict)
    top_k: int | None = Field(default=None, ge=1, le=50)


class SearchHit(BaseModel):
    chunk_id: UUID
    document_id: UUID
    filename: str
    section_path: list[str]
    content: str
    score: float
    vector_rank: int | None = None
    text_rank: int | None = None


# --- Accounts and chat history --------------------------------------------


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


class UserCreate(BaseModel):
    username: str = Field(min_length=2, max_length=128)
    password: str = Field(min_length=8, max_length=1024)
    display_name: str = Field(default="", max_length=255)
    is_admin: bool = True


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    username: str
    display_name: str
    is_admin: bool


class ChatMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    role: str
    content: str
    citations: list = Field(default_factory=list)
    refused: bool
    created_at: datetime


class ClearedHistory(BaseModel):
    workspace_id: UUID
    deleted: int
