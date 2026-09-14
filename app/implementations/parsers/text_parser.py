"""Plain text and markdown. Read directly -- no converter in the way."""

from __future__ import annotations

import asyncio
import re

from app.implementations.parsers import mime as mimes
from app.implementations.parsers.docling_blocks import extract_table_header
from app.interfaces.parser import DocumentParser
from app.interfaces.types import Block, BlockKind, ParsedDocument

_ATX_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*$")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")


def _read_text(path: str) -> str:
    with open(path, "rb") as fh:
        raw = fh.read()
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _flush(
    buffer: list[str], kind: BlockKind, section_path: list[str], blocks: list[Block]
) -> None:
    text = "\n".join(buffer).strip()
    if not text:
        buffer.clear()
        return
    header = extract_table_header(text) if kind is BlockKind.TABLE else None
    blocks.append(
        Block(
            kind=kind,
            text=text,
            section_path=list(section_path),
            table_header=header,
        )
    )
    buffer.clear()


def markdown_to_blocks(text: str) -> list[Block]:
    """Split markdown on headings, fenced code, tables and blank lines.

    Shared with any parser whose upstream hands back markdown.
    """
    blocks: list[Block] = []
    section_path: list[str] = []
    levels: list[int] = []
    buffer: list[str] = []
    kind = BlockKind.TEXT
    in_fence = False

    for line in text.splitlines():
        if _FENCE.match(line):
            if in_fence:
                buffer.append(line)
                _flush(buffer, BlockKind.CODE, section_path, blocks)
                in_fence = False
            else:
                _flush(buffer, kind, section_path, blocks)
                in_fence = True
                buffer.append(line)
            continue
        if in_fence:
            buffer.append(line)
            continue

        heading = _ATX_HEADING.match(line)
        if heading:
            _flush(buffer, kind, section_path, blocks)
            kind = BlockKind.TEXT
            level, title = len(heading.group(1)), heading.group(2).strip()
            while levels and levels[-1] >= level:
                levels.pop()
                section_path.pop()
            levels.append(level)
            section_path.append(title)
            blocks.append(
                Block(
                    kind=BlockKind.HEADING,
                    text=title,
                    section_path=list(section_path),
                    level=level,
                )
            )
            continue

        is_table_row = bool(_TABLE_ROW.match(line))
        if is_table_row and kind is not BlockKind.TABLE:
            _flush(buffer, kind, section_path, blocks)
            kind = BlockKind.TABLE
        elif not is_table_row and kind is BlockKind.TABLE:
            _flush(buffer, BlockKind.TABLE, section_path, blocks)
            kind = BlockKind.TEXT

        if not line.strip():
            _flush(buffer, kind, section_path, blocks)
            kind = BlockKind.TEXT
            continue
        buffer.append(line)

    _flush(buffer, BlockKind.CODE if in_fence else kind, section_path, blocks)
    return blocks


class TextParser(DocumentParser):
    mime_types = (mimes.TXT, mimes.MD)

    async def parse(self, file_path: str, mime_type: str) -> ParsedDocument:
        # File I/O and the line scan are blocking; keep them off the event loop.
        text = await asyncio.to_thread(_read_text, file_path)
        if mimes.normalise(mime_type) == mimes.MD:
            blocks = markdown_to_blocks(text)
        else:
            blocks = [
                Block(kind=BlockKind.TEXT, text=para.strip())
                for para in re.split(r"\n\s*\n", text)
                if para.strip()
            ]
        return ParsedDocument(
            markdown=text,
            blocks=blocks,
            page_count=None,
            metadata={"parser": "text", "mime_type": mime_type},
        )
