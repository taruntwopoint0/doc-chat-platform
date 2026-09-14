"""Vendor-neutral data structures passed between pipeline stages.

Nothing here imports a provider SDK. Implementations convert their own
vendor types into these on the way out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID


class DocumentStatus(StrEnum):
    PENDING = "pending"
    PARSING = "parsing"
    PROFILING = "profiling"
    CHUNKING = "chunking"
    EMBEDDING = "embedding"
    READY = "ready"
    FAILED = "failed"


class JobStatus(StrEnum):
    """Queue state, distinct from the pipeline stage the job is sitting in."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class BlockKind(StrEnum):
    HEADING = "heading"
    TEXT = "text"
    LIST = "list"
    TABLE = "table"
    CODE = "code"
    IMAGE = "image"
    SLIDE_NOTES = "slide_notes"


@dataclass(slots=True)
class Block:
    """One structural unit of a parsed document.

    `atomic` marks content the chunker must never split -- a numbered
    procedure, for instance. `table_header` holds the markdown header rows so
    a table spanning several chunks can repeat them.
    """

    kind: BlockKind
    text: str
    section_path: list[str] = field(default_factory=list)
    level: int | None = None
    page: int | None = None
    slide_index: int | None = None
    table_header: str | None = None
    atomic: bool = False


@dataclass(slots=True)
class ParsedDocument:
    markdown: str
    blocks: list[Block]
    page_count: int | None = None
    metadata: dict = field(default_factory=dict)


@dataclass(slots=True)
class ChunkDraft:
    """A chunk before it has an identity in the database."""

    ordinal: int
    content: str
    section_path: list[str]
    contextual_header: str | None = None
    token_count: int = 0
    page: int | None = None
    slide_index: int | None = None
    tags: dict = field(default_factory=dict)

    @property
    def embedding_text(self) -> str:
        """What the embedder sees: header + content. The UI shows content only."""
        if self.contextual_header:
            return f"{self.contextual_header}\n\n{self.content}"
        return self.content


@dataclass(slots=True)
class Chunk:
    """A chunk ready to be written to the vector store."""

    id: UUID
    document_id: UUID
    workspace_id: UUID
    ordinal: int
    content: str
    contextual_header: str | None
    section_path: list[str]
    tags: dict
    token_count: int
    embedding: list[float] | None = None


@dataclass(slots=True)
class SearchResult:
    chunk_id: UUID
    document_id: UUID
    content: str
    contextual_header: str | None
    section_path: list[str]
    score: float
    metadata: dict = field(default_factory=dict)


@dataclass(slots=True)
class StoredFile:
    key: str
    size_bytes: int
    sha256: str


@dataclass(slots=True)
class DocumentProfile:
    """LLM-derived description of a document. Values are never hardcoded --
    the vocabulary is whatever the corpus turns out to contain."""

    document_type: str = ""
    summary: str = ""
    topics: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "document_type": self.document_type,
            "summary": self.summary,
            "topics": self.topics,
            "entities": self.entities,
        }
