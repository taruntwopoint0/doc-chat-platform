"""Document parsing boundary."""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.interfaces.types import ParsedDocument


class DocumentParser(ABC):
    """Turns bytes on disk into a structured, vendor-neutral document.

    One implementation per family of formats. Adding a format means writing a
    new subclass and registering the MIME types it claims -- see
    app/implementations/parsers/__init__.py.
    """

    #: MIME types this parser handles. Used to build the registry.
    mime_types: tuple[str, ...] = ()

    @abstractmethod
    async def parse(self, file_path: str, mime_type: str) -> ParsedDocument:
        """Parse `file_path`, which is known to be of `mime_type`."""
        raise NotImplementedError
