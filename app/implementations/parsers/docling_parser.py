"""Docling-backed parser for PDF, DOCX, PPTX and HTML.

Docling is a heavy, synchronous, CPU-bound dependency. It is imported lazily so
the API and the test suite run without it, and every conversion is pushed onto a
worker thread so a long parse never stalls the event loop.
"""

from __future__ import annotations

import asyncio
import logging
from functools import lru_cache

from app.config import get_settings
from app.implementations.parsers import mime as mimes
from app.implementations.parsers.docling_blocks import (
    blocks_from_hybrid_chunker,
    blocks_from_items,
)
from app.interfaces.parser import DocumentParser
from app.interfaces.types import Block, BlockKind, ParsedDocument

logger = logging.getLogger(__name__)

#: One converter is expensive to build (it loads layout models), so it is
#: created once and reused. Docling's converter is thread-safe for conversion.
@lru_cache(maxsize=1)
def _converter():
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    settings = get_settings()
    pdf_options = PdfPipelineOptions()
    # OCR is a fallback for scanned pages: Docling runs it where it finds no
    # extractable text layer, so a born-digital PDF pays nothing for it.
    pdf_options.do_ocr = settings.ocr_enabled
    pdf_options.do_table_structure = True
    pdf_options.table_structure_options.do_cell_matching = True

    return DocumentConverter(
        allowed_formats=[
            InputFormat.PDF,
            InputFormat.DOCX,
            InputFormat.PPTX,
            InputFormat.HTML,
        ],
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pdf_options)},
    )


def _convert(file_path: str):
    result = _converter().convert(file_path)
    return result.document


def _page_count(doc: object, blocks: list[Block]) -> int | None:
    pages = getattr(doc, "pages", None)
    if pages:
        try:
            return len(pages)
        except TypeError:  # pragma: no cover - depends on docling version
            pass
    seen = {b.page for b in blocks if b.page is not None}
    return max(seen) if seen else None


def _speaker_notes(doc: object) -> list[Block]:
    """PPTX speaker notes, which Docling keeps outside the main body.

    Attached to their slide so the chunker keeps them in that slide's chunk.
    """
    notes: list[Block] = []
    texts = getattr(doc, "texts", None) or []
    for item in texts:
        label = str(getattr(getattr(item, "label", ""), "value", "") or "").lower()
        if label not in {"speaker_notes", "notes"}:
            continue
        text = str(getattr(item, "text", "") or "").strip()
        if not text:
            continue
        prov = getattr(item, "prov", None) or []
        page = next(
            (getattr(p, "page_no", None) for p in prov if getattr(p, "page_no", None)),
            None,
        )
        notes.append(
            Block(
                kind=BlockKind.SLIDE_NOTES,
                text=f"Speaker notes: {text}",
                page=page,
                slide_index=page,
            )
        )
    return notes


class DoclingParser(DocumentParser):
    mime_types = (mimes.PDF, mimes.DOCX, mimes.PPTX, mimes.HTML)

    async def parse(self, file_path: str, mime_type: str) -> ParsedDocument:
        normalised = mimes.normalise(mime_type) or mime_type
        is_slides = normalised == mimes.PPTX

        doc = await asyncio.to_thread(_convert, file_path)

        # Preferred: Docling's own structure-aware chunker. The pipeline chunker
        # then enforces the rules it cannot guarantee -- procedure integrity,
        # table header repetition, one chunk per slide -- and adds the overlap.
        blocks = await asyncio.to_thread(blocks_from_hybrid_chunker, doc, is_slides)
        strategy = "hybrid_chunker"
        if not blocks:
            blocks = await asyncio.to_thread(blocks_from_items, doc, is_slides)
            strategy = "iterate_items"

        if is_slides:
            blocks.extend(_speaker_notes(doc))
            blocks.sort(key=lambda b: (b.slide_index or 0))

        markdown = ""
        try:
            markdown = doc.export_to_markdown()  # type: ignore[attr-defined]
        except Exception as exc:  # pragma: no cover - depends on docling version
            logger.warning("export_to_markdown failed: %s", exc)
            markdown = "\n\n".join(b.text for b in blocks)

        return ParsedDocument(
            markdown=markdown,
            blocks=blocks,
            page_count=_page_count(doc, blocks),
            metadata={
                "parser": "docling",
                "mime_type": normalised,
                "block_strategy": strategy,
            },
        )
