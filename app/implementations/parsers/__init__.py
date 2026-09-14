"""Parser registry: MIME type -> DocumentParser.

Adding a format is one new DocumentParser subclass plus one line in
`build_registry`. Pipeline code asks the registry for a parser and never
imports a concrete class.

Which parser backs PDF/DOCX/PPTX/HTML is chosen by the PARSER env var:
`lite` (default, no ML dependencies) or `docling` (needs the `docling` extra).
"""

from __future__ import annotations

from app.implementations.parsers import mime as mimes
from app.implementations.parsers.legacy_office import LegacyOfficeParser
from app.implementations.parsers.lite_parser import LiteParser
from app.implementations.parsers.tabular_parser import TabularParser
from app.implementations.parsers.text_parser import TextParser
from app.interfaces.parser import DocumentParser


class ParserRegistry:
    """Immutable-after-construction map from MIME type to a parser instance."""

    def __init__(self) -> None:
        self._parsers: dict[str, DocumentParser] = {}

    def register(self, parser: DocumentParser) -> None:
        if not parser.mime_types:
            raise ValueError(f"{type(parser).__name__} declares no mime_types")
        for mime_type in parser.mime_types:
            if mime_type in self._parsers:
                existing = type(self._parsers[mime_type]).__name__
                raise ValueError(
                    f"{mime_type} is already handled by {existing}; "
                    "a MIME type maps to exactly one parser"
                )
            self._parsers[mime_type] = parser

    def get(self, mime_type: str) -> DocumentParser:
        normalised = mimes.normalise(mime_type) or mime_type
        parser = self._parsers.get(normalised)
        if parser is None:
            raise mimes.UnsupportedFileType(mime_type, "<upload>")
        return parser

    def supports(self, mime_type: str) -> bool:
        return (mimes.normalise(mime_type) or mime_type) in self._parsers

    @property
    def mime_types(self) -> tuple[str, ...]:
        return tuple(sorted(self._parsers))


def _rich_parser(name: str) -> DocumentParser:
    """The parser for PDF/DOCX/PPTX/HTML, per the PARSER setting."""
    if name == "lite":
        return LiteParser()
    if name == "docling":
        # Imported only on this branch: docling pulls PyTorch, which the default
        # install and the Docker image deliberately exclude.
        from app.implementations.parsers.docling_parser import DoclingParser

        return DoclingParser()
    raise ValueError(f"Unknown PARSER '{name}'. Expected 'lite' or 'docling'.")


def build_registry(parser_impl: str | None = None) -> ParserRegistry:
    """Wire every parser. The one place the format table is expressed in code."""
    if parser_impl is None:
        from app.config import get_settings

        parser_impl = get_settings().parser_impl

    registry = ParserRegistry()
    registry.register(_rich_parser(parser_impl.strip().lower()))
    registry.register(TabularParser())
    registry.register(TextParser())
    # Legacy binary formats are converted to their modern equivalent first, then
    # handed back to the registry, so it needs a reference to itself.
    registry.register(LegacyOfficeParser(registry))
    return registry


__all__ = ["ParserRegistry", "build_registry"]
