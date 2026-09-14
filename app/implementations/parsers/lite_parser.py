"""The default parser: per-format libraries, no ML models, no PyTorch.

Covers the same MIME types DoclingParser does and returns the same
ParsedDocument shape, so the two are interchangeable behind the registry --
selected by the PARSER env var.

What it gives up versus Docling: no OCR, so a scanned PDF with no text layer
yields nothing, and PDF heading detection falls back to font-size heuristics
when the file carries no outline. A workspace that needs either should run
PARSER=docling with the `docling` extra installed.
"""

from __future__ import annotations

import asyncio

from app.implementations.parsers import mime as mimes
from app.implementations.parsers.lite_html import parse_html
from app.implementations.parsers.lite_office import parse_docx, parse_pptx
from app.interfaces.parser import DocumentParser
from app.interfaces.types import ParsedDocument


def _parse_pdf(file_path: str):
    # Imported here so the module loads even where PyMuPDF is absent.
    from app.implementations.parsers.lite_pdf import parse_pdf

    return parse_pdf(file_path)


class LiteParser(DocumentParser):
    mime_types = (mimes.PDF, mimes.DOCX, mimes.PPTX, mimes.HTML)

    #: MIME -> a callable returning (blocks, page_count, markdown).
    _BACKENDS = {
        mimes.PDF: _parse_pdf,
        mimes.DOCX: parse_docx,
        mimes.PPTX: parse_pptx,
        mimes.HTML: parse_html,
    }

    async def parse(self, file_path: str, mime_type: str) -> ParsedDocument:
        normalised = mimes.normalise(mime_type) or mime_type
        backend = self._BACKENDS.get(normalised)
        if backend is None:
            raise mimes.UnsupportedFileType(mime_type, file_path)

        # Every backend is synchronous and CPU-bound; keep it off the loop.
        blocks, page_count, markdown = await asyncio.to_thread(backend, file_path)

        return ParsedDocument(
            markdown=markdown,
            blocks=blocks,
            page_count=page_count,
            metadata={
                "parser": "lite",
                "mime_type": normalised,
                "backend": backend.__name__,
                "ocr": False,
            },
        )
