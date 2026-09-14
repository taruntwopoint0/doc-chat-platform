"""Parser registry routing and MIME detection.

Routing is by MIME type rather than extension, so these tests feed real file
signatures rather than trusting a filename.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from app.implementations.parsers import ParserRegistry, build_registry
from app.implementations.parsers import mime as mimes
from app.implementations.parsers.legacy_office import LegacyOfficeParser
from app.implementations.parsers.lite_parser import LiteParser
from app.implementations.parsers.tabular_parser import TabularParser
from app.implementations.parsers.text_parser import TextParser
from app.interfaces.parser import DocumentParser
from app.interfaces.types import ParsedDocument

pytestmark = pytest.mark.no_db


@pytest.fixture(scope="module")
def registry() -> ParserRegistry:
    return build_registry("lite")


EXPECTED = {
    mimes.PDF: LiteParser,
    mimes.DOCX: LiteParser,
    mimes.PPTX: LiteParser,
    mimes.HTML: LiteParser,
    mimes.CSV: TabularParser,
    mimes.XLSX: TabularParser,
    mimes.TXT: TextParser,
    mimes.MD: TextParser,
    mimes.DOC: LegacyOfficeParser,
    mimes.PPT: LegacyOfficeParser,
    mimes.XLS: LegacyOfficeParser,
}


@pytest.mark.parametrize("mime_type,parser_class", sorted(EXPECTED.items()))
def test_each_mime_type_routes_to_its_parser(registry, mime_type, parser_class):
    assert isinstance(registry.get(mime_type), parser_class)


def test_every_supported_type_has_a_parser(registry):
    missing = [m for m in mimes.SUPPORTED_MIME_TYPES if not registry.supports(m)]
    assert missing == []


def test_unsupported_type_is_rejected_with_the_accepted_list(registry):
    with pytest.raises(mimes.UnsupportedFileType) as excinfo:
        registry.get("application/x-tar")
    message = str(excinfo.value)
    assert ".pdf" in message and ".docx" in message and ".csv" in message


def test_a_mime_type_cannot_be_claimed_twice():
    class Duplicate(DocumentParser):
        mime_types = (mimes.PDF,)

        async def parse(self, file_path: str, mime_type: str) -> ParsedDocument:
            raise NotImplementedError

    registry = ParserRegistry()
    registry.register(LiteParser())
    with pytest.raises(ValueError, match="already handled by"):
        registry.register(Duplicate())


def test_a_parser_must_declare_mime_types():
    class Silent(DocumentParser):
        async def parse(self, file_path: str, mime_type: str) -> ParsedDocument:
            raise NotImplementedError

    with pytest.raises(ValueError, match="declares no mime_types"):
        ParserRegistry().register(Silent())


def test_adding_a_format_is_one_class_and_one_registration():
    """The extension point the design promises: no registry surgery needed."""

    class RtfParser(DocumentParser):
        mime_types = ("application/rtf",)

        async def parse(self, file_path: str, mime_type: str) -> ParsedDocument:
            return ParsedDocument(markdown="", blocks=[])

    registry = build_registry("lite")
    registry.register(RtfParser())
    assert isinstance(registry.get("application/rtf"), RtfParser)


# --- MIME detection ------------------------------------------------------


def _ooxml(folder: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr(f"{folder}/document.xml", "<x/>")
    return buffer.getvalue()


def test_pdf_is_detected_from_its_signature_not_its_name():
    assert mimes.detect_mime(b"%PDF-1.7\n...", "invoice.txt", "text/plain") == mimes.PDF


@pytest.mark.parametrize(
    "folder,expected",
    [("word", mimes.DOCX), ("ppt", mimes.PPTX), ("xl", mimes.XLSX)],
)
def test_ooxml_family_is_read_from_the_zip_directory(folder, expected):
    # All three share the PK.. signature, so the archive must be looked inside.
    assert mimes.detect_mime(_ooxml(folder), "file.bin", None) == expected


def test_html_is_detected_from_its_doctype():
    assert mimes.detect_mime(b"<!DOCTYPE html><html>", "page", None) == mimes.HTML


def test_legacy_ole2_uses_the_declared_type_to_disambiguate():
    ole = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64
    assert mimes.detect_mime(ole, "report.doc", None) == mimes.DOC
    assert mimes.detect_mime(ole, "deck.ppt", None) == mimes.PPT
    assert mimes.detect_mime(ole, "sheet.xls", None) == mimes.XLS


def test_text_extension_decides_between_csv_md_and_txt():
    data = b"Name,Role\nA,B\n"
    assert mimes.detect_mime(data, "people.csv", None) == mimes.CSV
    assert mimes.detect_mime(b"# Title", "notes.md", None) == mimes.MD
    assert mimes.detect_mime(b"plain words", "notes.txt", None) == mimes.TXT


def test_binary_rubbish_is_rejected():
    with pytest.raises(mimes.UnsupportedFileType):
        mimes.detect_mime(b"\x00\x01\x02\x03\xff\xfe", "mystery.bin", None)


def test_declared_content_type_aliases_are_normalised():
    assert mimes.normalise("application/x-pdf") == mimes.PDF
    assert mimes.normalise("text/csv; charset=utf-8") == mimes.CSV
    assert mimes.normalise("application/x-tar") is None


def test_docling_parser_is_selectable_by_config():
    """PARSER=docling must at least be a recognised choice.

    The import itself is skipped when the optional extra is not installed --
    that is the point of the extra.
    """
    pytest.importorskip("docling", reason="docling extra not installed")
    from app.implementations.parsers.docling_parser import DoclingParser

    assert isinstance(build_registry("docling").get(mimes.PDF), DoclingParser)


def test_unknown_parser_name_fails_loudly():
    with pytest.raises(ValueError, match="Unknown PARSER"):
        build_registry("magic")
