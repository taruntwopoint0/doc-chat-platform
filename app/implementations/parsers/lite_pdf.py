"""PDF blocks via PyMuPDF.

Heading hierarchy comes from the PDF outline when the file has one, since that
is the author's own structure. Without an outline, headings are inferred from
font size relative to the document's body text -- crude, but it is the only
signal a PDF reliably carries.

No OCR: a scanned page with no text layer yields nothing here. Route such a
corpus to the Docling parser (PARSER=docling, docling extra installed).
"""

from __future__ import annotations

from collections import Counter

import pymupdf

from app.interfaces.types import Block, BlockKind

#: A span must exceed body size by this factor to read as a heading.
_HEADING_RATIO = 1.15
#: Headings are short; a long line at heading size is a pull quote, not a title.
_MAX_HEADING_CHARS = 200


def _body_font_size(doc: pymupdf.Document) -> float:
    """Modal font size across the document, sampled from the first pages."""
    sizes: Counter[float] = Counter()
    for page in doc[: min(len(doc), 20)]:
        for block in page.get_text("dict").get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    text = span.get("text", "").strip()
                    if text:
                        sizes[round(span.get("size", 0), 1)] += len(text)
    return sizes.most_common(1)[0][0] if sizes else 10.0


def _heading_level(size: float, body: float, scale: list[float]) -> int:
    """Rank a heading size against the sizes seen, largest = level 1."""
    for index, candidate in enumerate(scale):
        if abs(size - candidate) < 0.5:
            return index + 1
    return len(scale) + 1 if size > body else 6


def _table_markdown(table) -> tuple[str, str | None]:
    rows = table.extract()
    cleaned = [
        [("" if cell is None else str(cell).replace("\n", " ").strip()) for cell in row]
        for row in rows
        if any(cell not in (None, "") for cell in row)
    ]
    if not cleaned:
        return "", None
    header = cleaned[0]
    separator = ["---"] * len(header)
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(separator) + " |",
    ]
    header_block = "\n".join(lines)
    for row in cleaned[1:]:
        padded = row + [""] * (len(header) - len(row))
        lines.append("| " + " | ".join(padded[: len(header)]) + " |")
    return "\n".join(lines), header_block


def _page_tables(page) -> list[tuple[str, str | None, tuple[float, float, float, float]]]:
    try:
        finder = page.find_tables()
    except Exception:  # pragma: no cover - depends on page content
        return []
    found = []
    for table in getattr(finder, "tables", []):
        markdown, header = _table_markdown(table)
        if markdown:
            found.append((markdown, header, tuple(table.bbox)))
    return found


def _in_any_table(bbox, table_boxes) -> bool:
    x0, y0, x1, y1 = bbox
    for tx0, ty0, tx1, ty1 in table_boxes:
        if x0 >= tx0 - 2 and y0 >= ty0 - 2 and x1 <= tx1 + 2 and y1 <= ty1 + 2:
            return True
    return False


def _outline_sections(doc: pymupdf.Document) -> dict[int, list[tuple[int, str]]]:
    """page number -> [(level, title)] from the PDF outline, if present."""
    sections: dict[int, list[tuple[int, str]]] = {}
    try:
        toc = doc.get_toc()
    except Exception:  # pragma: no cover
        return sections
    for level, title, page_no in toc or []:
        title = (title or "").strip()
        if title:
            sections.setdefault(int(page_no), []).append((int(level), title))
    return sections


def _apply_heading(path: list[str], levels: list[int], level: int, title: str) -> None:
    while levels and levels[-1] >= level:
        levels.pop()
        path.pop()
    levels.append(level)
    path.append(title)


def parse_pdf(file_path: str) -> tuple[list[Block], int, str]:
    """Return (blocks, page_count, markdown)."""
    blocks: list[Block] = []
    markdown: list[str] = []
    section_path: list[str] = []
    levels: list[int] = []

    with pymupdf.open(file_path) as doc:
        page_count = len(doc)
        body_size = _body_font_size(doc)
        outline = _outline_sections(doc)
        # Heading sizes present in the document, largest first, so a size can be
        # ranked into a level.
        scale = sorted(
            {
                round(span["size"], 1)
                for page in doc[: min(len(doc), 20)]
                for blk in page.get_text("dict").get("blocks", [])
                for line in blk.get("lines", [])
                for span in line.get("spans", [])
                if span.get("text", "").strip()
                and span.get("size", 0) > body_size * _HEADING_RATIO
            },
            reverse=True,
        )

        for page_index, page in enumerate(doc, start=1):
            for level, title in outline.get(page_index, []):
                _apply_heading(section_path, levels, level, title)
                blocks.append(
                    Block(
                        kind=BlockKind.HEADING,
                        text=title,
                        section_path=list(section_path),
                        level=level,
                        page=page_index,
                    )
                )
                markdown.append(f"{'#' * min(level, 6)} {title}")

            tables = _page_tables(page)
            table_boxes = [bbox for _, _, bbox in tables]

            for raw in page.get_text("dict").get("blocks", []):
                if raw.get("type") != 0:
                    # TODO(milestone-3): caption embedded images with a vision
                    # model. Until then they contribute no text.
                    continue
                if _in_any_table(raw.get("bbox", (0, 0, 0, 0)), table_boxes):
                    continue
                lines = raw.get("lines", [])
                text = "\n".join(
                    "".join(span.get("text", "") for span in line.get("spans", []))
                    for line in lines
                ).strip()
                if not text:
                    continue
                spans = [s for line in lines for s in line.get("spans", [])]
                max_size = max(
                    (s.get("size", 0) for s in spans), default=body_size
                )
                is_heading = (
                    not outline
                    and max_size > body_size * _HEADING_RATIO
                    and len(text) <= _MAX_HEADING_CHARS
                    and "\n" not in text
                )
                if is_heading:
                    level = _heading_level(round(max_size, 1), body_size, scale)
                    _apply_heading(section_path, levels, level, text)
                    blocks.append(
                        Block(
                            kind=BlockKind.HEADING,
                            text=text,
                            section_path=list(section_path),
                            level=level,
                            page=page_index,
                        )
                    )
                    markdown.append(f"{'#' * min(level, 6)} {text}")
                    continue

                blocks.append(
                    Block(
                        kind=BlockKind.TEXT,
                        text=text,
                        section_path=list(section_path),
                        page=page_index,
                    )
                )
                markdown.append(text)

            for table_md, header, _ in tables:
                blocks.append(
                    Block(
                        kind=BlockKind.TABLE,
                        text=table_md,
                        section_path=list(section_path),
                        page=page_index,
                        table_header=header,
                    )
                )
                markdown.append(table_md)

    return blocks, page_count, "\n\n".join(markdown)
