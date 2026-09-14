"""HTML blocks via the standard library's HTMLParser.

Docling handled HTML before; it moved to the optional extra, and pulling in
BeautifulSoup or lxml just for this would undo the point of a lite parser. The
stdlib parser covers the structure that matters here: headings, block text,
lists and tables.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

from app.interfaces.types import Block, BlockKind

_HEADINGS = {f"h{n}": n for n in range(1, 7)}
_BLOCK_TAGS = {"p", "div", "section", "article", "blockquote", "pre", "figcaption"}
_SKIP_TAGS = {"script", "style", "noscript", "head", "svg", "template"}
_WHITESPACE = re.compile(r"[ \t\r\f\v]+")


def _clean(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


class _Collector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[Block] = []
        self.markdown: list[str] = []
        self._section_path: list[str] = []
        self._levels: list[int] = []
        self._buffer: list[str] = []
        self._skip_depth = 0
        self._heading_level: int | None = None
        self._list_depth = 0
        # Table state: rows of cells, plus where the current cell's text goes.
        self._table_rows: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    # -- helpers -------------------------------------------------------
    def _emit(self, kind: BlockKind, text: str, **kwargs) -> None:
        text = text.strip()
        if text:
            self.blocks.append(
                Block(
                    kind=kind,
                    text=text,
                    section_path=list(self._section_path),
                    **kwargs,
                )
            )
            self.markdown.append(text)

    def _flush_text(self, kind: BlockKind = BlockKind.TEXT) -> None:
        text = _clean(" ".join(self._buffer))
        self._buffer.clear()
        if text:
            self._emit(kind, text)

    def _push_heading(self, level: int, title: str) -> None:
        while self._levels and self._levels[-1] >= level:
            self._levels.pop()
            self._section_path.pop()
        self._levels.append(level)
        self._section_path.append(title)
        self.blocks.append(
            Block(
                kind=BlockKind.HEADING,
                text=title,
                section_path=list(self._section_path),
                level=level,
            )
        )
        self.markdown.append(f"{'#' * min(level, 6)} {title}")

    def _finish_table(self) -> None:
        from app.implementations.parsers.lite_office import _rows_to_markdown

        rows = self._table_rows or []
        self._table_rows = None
        table_md, header = _rows_to_markdown(rows)
        if table_md:
            self.blocks.append(
                Block(
                    kind=BlockKind.TABLE,
                    text=table_md,
                    section_path=list(self._section_path),
                    table_header=header,
                )
            )
            self.markdown.append(table_md)

    # -- HTMLParser hooks ----------------------------------------------
    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "br":
            self._buffer.append("\n")
            return
        if tag in _HEADINGS:
            self._flush_text()
            self._heading_level = _HEADINGS[tag]
        elif tag == "table":
            self._flush_text()
            self._table_rows = []
        elif tag == "tr" and self._table_rows is not None:
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []
        elif tag in {"ul", "ol"}:
            self._flush_text()
            self._list_depth += 1
        elif tag == "li":
            self._flush_text(BlockKind.LIST if self._list_depth else BlockKind.TEXT)
        elif tag in _BLOCK_TAGS:
            self._flush_text()

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag in _HEADINGS:
            title = _clean(" ".join(self._buffer))
            self._buffer.clear()
            if title and self._heading_level is not None:
                self._push_heading(self._heading_level, title)
            self._heading_level = None
        elif tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append(_clean(" ".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._table_rows is not None:
                self._table_rows.append(self._row)
            self._row = None
        elif tag == "table" and self._table_rows is not None:
            self._finish_table()
        elif tag in {"ul", "ol"}:
            self._flush_text(BlockKind.LIST if self._list_depth else BlockKind.TEXT)
            self._list_depth = max(0, self._list_depth - 1)
        elif tag == "li":
            self._flush_text(BlockKind.LIST)
        elif tag in _BLOCK_TAGS:
            self._flush_text()

    def handle_data(self, data: str) -> None:
        if self._skip_depth or not data.strip():
            return
        if self._cell is not None:
            self._cell.append(data)
        elif self._row is not None or self._table_rows is not None:
            return  # stray text between cells
        else:
            self._buffer.append(data)

    def close(self) -> None:  # type: ignore[override]
        super().close()
        self._flush_text()


def parse_html(file_path: str) -> tuple[list[Block], int | None, str]:
    with open(file_path, "rb") as fh:
        raw = fh.read()
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:  # pragma: no cover - latin-1 decodes any byte string
        text = raw.decode("utf-8", errors="replace")

    collector = _Collector()
    collector.feed(text)
    collector.close()
    return collector.blocks, None, "\n\n".join(collector.markdown)
