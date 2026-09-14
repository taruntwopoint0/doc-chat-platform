"""Turn parsed blocks into chunks.

Packs blocks up to a token target, with overlap, subject to four hard rules:

* a numbered procedure is never split mid-sequence;
* a table always carries its header row, repeated on every part it spans;
* PPTX produces exactly one chunk per slide;
* no chunk exceeds the embedder's input limit -- the one rule that can
  override the others, since a chunk too large to embed is worse than a split.

Headings are not chunks of their own; they set the section path and are carried
into the chunk that follows them, so retrieved text keeps its context.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.interfaces.types import Block, BlockKind, ChunkDraft
from app.pipeline.procedures import group_numbered_procedures
from app.pipeline.splitting import (
    procedure_parts,
    sentence_parts,
    table_parts,
    tail_overlap,
)
from app.pipeline.tokens import count_tokens


@dataclass(frozen=True)
class ChunkingConfig:
    target_tokens: int = 500
    overlap_ratio: float = 0.15
    #: Hard ceiling, from the embedding model's input limit.
    max_tokens: int = 8192

    @property
    def overlap_tokens(self) -> int:
        return int(self.target_tokens * self.overlap_ratio)


class _Assembler:
    """Accumulates blocks into chunks and emits ChunkDrafts in order."""

    def __init__(self, config: ChunkingConfig) -> None:
        self.config = config
        self.drafts: list[ChunkDraft] = []
        self._buffer: list[str] = []
        self._tokens = 0
        self._section_path: list[str] = []
        self._page: int | None = None
        self._slide: int | None = None
        self._pending_overlap = ""

    def _start(self, block: Block) -> None:
        if not self._buffer:
            self._section_path = list(block.section_path)
            self._page = block.page
            self._slide = block.slide_index
            if self._pending_overlap:
                self._buffer.append(self._pending_overlap)
                self._tokens += count_tokens(self._pending_overlap)
                self._pending_overlap = ""

    def add(self, block: Block, text: str | None = None) -> None:
        body = block.text if text is None else text
        if not body.strip():
            return
        tokens = count_tokens(body)
        if self._buffer and self._tokens + tokens > self.config.target_tokens:
            self.flush()
        self._start(block)
        self._buffer.append(body)
        self._tokens += tokens

    def emit_standalone(self, block: Block, text: str) -> None:
        """Emit `text` as its own chunk without disturbing the buffer's overlap."""
        self.flush(carry_overlap=False)
        # A table part leads with its repeated header, and a continued procedure
        # with its marker; prose carried over from the previous chunk would sit
        # in front of both.
        self._pending_overlap = ""
        self._start(block)
        self._buffer.append(text)
        self._tokens = count_tokens(text)
        self.flush(carry_overlap=False)

    def flush(self, carry_overlap: bool = True) -> None:
        if not self._buffer:
            return
        content = "\n\n".join(self._buffer).strip()
        self._buffer = []
        self._tokens = 0
        if not content:
            return
        # Backstop: packing plus carried overlap can nudge a chunk past the
        # ceiling. A chunk the embedder will reject is worse than an extra
        # split, so enforce the limit unconditionally here.
        pieces = (
            [content]
            if count_tokens(content) <= self.config.max_tokens
            else sentence_parts(content, self.config.max_tokens)
        )
        for piece in pieces:
            self.drafts.append(
                ChunkDraft(
                    ordinal=len(self.drafts),
                    content=piece,
                    section_path=list(self._section_path),
                    token_count=count_tokens(piece),
                    page=self._page,
                    slide_index=self._slide,
                )
            )
        self._pending_overlap = (
            tail_overlap(content, self.config.overlap_tokens) if carry_overlap else ""
        )


def _chunk_slides(blocks: list[Block], config: ChunkingConfig) -> list[ChunkDraft]:
    """One chunk per slide. Slides never merge and never overlap each other."""
    order: list[int] = []
    grouped: dict[int, list[Block]] = {}
    for block in blocks:
        key = block.slide_index or 0
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(block)

    drafts: list[ChunkDraft] = []
    for slide in order:
        slide_blocks = grouped[slide]
        content = "\n\n".join(b.text for b in slide_blocks if b.text.strip()).strip()
        if not content:
            continue
        section_path = next(
            (list(b.section_path) for b in slide_blocks if b.section_path), []
        )
        tokens = count_tokens(content)
        if tokens <= config.max_tokens:
            parts = [content]
        else:
            # Only the embedder's hard limit can break the one-chunk-per-slide
            # rule; a very dense slide is split at sentence boundaries.
            parts = sentence_parts(content, config.max_tokens)
        for part in parts:
            drafts.append(
                ChunkDraft(
                    ordinal=len(drafts),
                    content=part,
                    section_path=section_path,
                    token_count=count_tokens(part),
                    page=slide,
                    slide_index=slide,
                )
            )
    return drafts


def chunk_blocks(blocks: list[Block], config: ChunkingConfig) -> list[ChunkDraft]:
    blocks = [b for b in blocks if b.text and b.text.strip()]
    if not blocks:
        return []
    if any(b.slide_index is not None for b in blocks):
        return _chunk_slides(blocks, config)

    blocks = group_numbered_procedures(blocks)
    assembler = _Assembler(config)

    for block in blocks:
        tokens = count_tokens(block.text)

        if block.kind is BlockKind.TABLE and block.table_header:
            if tokens > config.target_tokens:
                for part in table_parts(block, config.target_tokens):
                    assembler.emit_standalone(block, part)
            else:
                assembler.add(block)
            continue

        if block.atomic:
            if tokens > config.max_tokens:
                for part in procedure_parts(block.text, config.max_tokens):
                    assembler.emit_standalone(block, part)
            else:
                # Fits the ceiling, so it stays whole even if it overshoots the
                # target -- keeping the sequence intact matters more.
                assembler.add(block)
            continue

        if tokens > config.target_tokens:
            for part in sentence_parts(block.text, config.target_tokens):
                assembler.add(block, part)
            continue

        assembler.add(block)

    assembler.flush()
    return assembler.drafts
