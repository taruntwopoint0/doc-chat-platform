"""Convert a DoclingDocument into vendor-neutral Blocks.

Two paths, in order of preference:

1. HybridChunker -- Docling's own structure-aware chunker. Its chunks already
   respect element boundaries and carry heading provenance, so each becomes one
   Block. The pipeline chunker then applies the rules HybridChunker does not
   guarantee (procedure integrity, table header repetition, slide isolation)
   and the overlap.
2. iterate_items -- a flat element walk, used when HybridChunker is
   unavailable or yields nothing (very short documents, chunker import errors).

Docling types are imported lazily so the rest of the app runs without it.
"""

from __future__ import annotations

import logging

from app.interfaces.types import Block, BlockKind

logger = logging.getLogger(__name__)

_HEADING_LABELS = {"title", "section_header"}
_TABLE_LABELS = {"table", "document_index"}
_IMAGE_LABELS = {"picture", "figure"}
_CODE_LABELS = {"code", "formula"}


def _label_of(item: object) -> str:
    label = getattr(item, "label", "")
    return str(getattr(label, "value", label) or "").lower()


def _page_of(item: object) -> int | None:
    prov = getattr(item, "prov", None) or []
    for entry in prov:
        page = getattr(entry, "page_no", None)
        if page is not None:
            return int(page)
    return None


def _table_markdown(item: object, doc: object) -> tuple[str, str | None]:
    """Render a table as markdown and return (full_table, header_rows).

    The header is returned separately so the chunker can repeat it when a long
    table has to span several chunks.
    """
    text = ""
    try:
        text = item.export_to_markdown(doc)  # type: ignore[attr-defined]
    except TypeError:
        try:
            text = item.export_to_markdown()  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover - depends on docling version
            text = ""
    except Exception:  # pragma: no cover
        text = ""
    if not text:
        text = str(getattr(item, "text", "") or "")
    return text, extract_table_header(text)


def extract_table_header(markdown_table: str) -> str | None:
    """Return the header row plus separator of a markdown table, if present."""
    lines = [ln for ln in markdown_table.splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    header, separator = lines[0], lines[1]
    stripped = separator.replace("|", "").replace(":", "").replace("-", "").strip()
    if "|" in header and "-" in separator and not stripped:
        return f"{header}\n{separator}"
    return None


def blocks_from_items(doc: object, is_slides: bool) -> list[Block]:
    """Flat element walk. Tracks the heading breadcrumb as it descends."""
    blocks: list[Block] = []
    section_path: list[str] = []
    heading_levels: list[int] = []

    for item, level in doc.iterate_items():  # type: ignore[attr-defined]
        label = _label_of(item)
        text = str(getattr(item, "text", "") or "").strip()
        page = _page_of(item)
        slide = page if is_slides else None

        if label in _HEADING_LABELS:
            if not text:
                continue
            while heading_levels and heading_levels[-1] >= level:
                heading_levels.pop()
                section_path.pop()
            heading_levels.append(level)
            section_path.append(text)
            blocks.append(
                Block(
                    kind=BlockKind.HEADING,
                    text=text,
                    section_path=list(section_path),
                    level=level,
                    page=page,
                    slide_index=slide,
                )
            )
            continue

        if label in _TABLE_LABELS:
            table_md, header = _table_markdown(item, doc)
            if not table_md.strip():
                continue
            blocks.append(
                Block(
                    kind=BlockKind.TABLE,
                    text=table_md,
                    section_path=list(section_path),
                    page=page,
                    slide_index=slide,
                    table_header=header,
                )
            )
            continue

        if label in _IMAGE_LABELS:
            caption = ""
            try:
                caption = str(item.caption_text(doc) or "").strip()  # type: ignore[attr-defined]
            except Exception:  # pragma: no cover - optional in some versions
                caption = text
            # TODO(milestone-3): replace with a vision-model caption of the image
            # itself. Until then a placeholder keeps ordinals and section paths
            # stable so re-ingesting later does not renumber every chunk.
            placeholder = f"[image: {caption}]" if caption else "[image]"
            blocks.append(
                Block(
                    kind=BlockKind.IMAGE,
                    text=placeholder,
                    section_path=list(section_path),
                    page=page,
                    slide_index=slide,
                )
            )
            continue

        if not text:
            continue

        kind = BlockKind.CODE if label in _CODE_LABELS else BlockKind.TEXT
        if label == "list_item":
            kind = BlockKind.LIST
        blocks.append(
            Block(
                kind=kind,
                text=text,
                section_path=list(section_path),
                page=page,
                slide_index=slide,
            )
        )

    return blocks


def blocks_from_hybrid_chunker(doc: object, is_slides: bool) -> list[Block]:
    """Use Docling's HybridChunker; returns [] if it is unusable here."""
    try:
        from docling.chunking import HybridChunker
    except Exception as exc:  # pragma: no cover - depends on install
        logger.info("HybridChunker unavailable (%s); using element walk", exc)
        return []

    try:
        chunker = HybridChunker()
        chunks = list(chunker.chunk(dl_doc=doc))
    except Exception as exc:  # pragma: no cover - depends on docling version
        logger.warning("HybridChunker failed (%s); using element walk", exc)
        return []

    blocks: list[Block] = []
    for chunk in chunks:
        text = (getattr(chunk, "text", "") or "").strip()
        if not text:
            continue
        meta = getattr(chunk, "meta", None)
        headings = list(getattr(meta, "headings", None) or [])
        doc_items = list(getattr(meta, "doc_items", None) or [])

        page = next((p for p in (_page_of(i) for i in doc_items) if p is not None), None)
        labels = {_label_of(i) for i in doc_items}
        if labels & _TABLE_LABELS:
            kind = BlockKind.TABLE
            header = extract_table_header(text)
        else:
            kind = BlockKind.LIST if "list_item" in labels else BlockKind.TEXT
            header = None

        blocks.append(
            Block(
                kind=kind,
                text=text,
                section_path=headings,
                page=page,
                slide_index=page if is_slides else None,
                table_header=header,
            )
        )
    return blocks
