"""MIME detection and the canonical list of accepted types.

Routing is by MIME type, not extension: a client's declared content type is a
hint, and an extension is a weaker hint still, so magic bytes win where they
are decisive. ZIP- and OLE2-based Office formats share a signature, so those
fall back to the declared type or extension to pick the family member.

Deliberately dependency-free -- python-magic needs libmagic, which is awkward
on Windows dev machines and adds nothing over the handful of signatures below.
"""

from __future__ import annotations

import mimetypes
import zipfile
from io import BytesIO

PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DOC = "application/msword"
PPT = "application/vnd.ms-powerpoint"
XLS = "application/vnd.ms-excel"
CSV = "text/csv"
TXT = "text/plain"
MD = "text/markdown"
HTML = "text/html"

#: Extension -> MIME, used when sniffing is inconclusive. Not the primary path.
_EXTENSION_MAP = {
    ".pdf": PDF,
    ".docx": DOCX,
    ".pptx": PPTX,
    ".xlsx": XLSX,
    ".xlsm": XLSX,
    ".doc": DOC,
    ".ppt": PPT,
    ".xls": XLS,
    ".csv": CSV,
    ".tsv": CSV,
    ".txt": TXT,
    ".md": MD,
    ".markdown": MD,
    ".html": HTML,
    ".htm": HTML,
}

#: Aliases browsers and clients send that mean one of our canonical types.
_ALIASES = {
    "application/x-pdf": PDF,
    "text/x-markdown": MD,
    "application/csv": CSV,
    "text/comma-separated-values": CSV,
    "text/tab-separated-values": CSV,
    "application/vnd.ms-excel.sheet.macroenabled.12": XLSX,
    "application/xhtml+xml": HTML,
}

_OLE2_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

#: Presented to the user when an upload is rejected.
SUPPORTED_MIME_TYPES: tuple[str, ...] = (
    PDF, DOCX, PPTX, XLSX, DOC, PPT, XLS, CSV, TXT, MD, HTML,
)

SUPPORTED_EXTENSIONS: tuple[str, ...] = tuple(sorted(_EXTENSION_MAP))


class UnsupportedFileType(ValueError):
    """Raised at upload time; the message lists what is accepted."""

    def __init__(self, detected: str, filename: str) -> None:
        accepted = ", ".join(SUPPORTED_EXTENSIONS)
        super().__init__(
            f"Unsupported file type for '{filename}' (detected: {detected}). "
            f"Accepted file types: {accepted}."
        )
        self.detected = detected


def normalise(mime_type: str | None) -> str | None:
    if not mime_type:
        return None
    base = mime_type.split(";", 1)[0].strip().lower()
    base = _ALIASES.get(base, base)
    return base if base in SUPPORTED_MIME_TYPES else None


def _from_extension(filename: str) -> str | None:
    lowered = filename.lower()
    for ext, mime in _EXTENSION_MAP.items():
        if lowered.endswith(ext):
            return mime
    guessed, _ = mimetypes.guess_type(filename)
    return normalise(guessed)


def _sniff_ooxml(data: bytes, hint: str | None) -> str | None:
    """OOXML files are ZIPs; the top-level directory names the family."""
    try:
        with zipfile.ZipFile(BytesIO(data)) as zf:
            names = zf.namelist()
    except (zipfile.BadZipFile, OSError):
        return None
    if any(n.startswith("word/") for n in names):
        return DOCX
    if any(n.startswith("ppt/") for n in names):
        return PPTX
    if any(n.startswith("xl/") for n in names):
        return XLSX
    return hint


def _looks_like_html(head: bytes) -> bool:
    probe = head[:1024].lstrip().lower()
    return probe.startswith((b"<!doctype html", b"<html", b"<?xml-stylesheet"))


def _is_text(data: bytes) -> bool:
    sample = data[:8192]
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        try:
            sample.decode("latin-1")
        except UnicodeDecodeError:
            return False
    return True


def detect_mime(data: bytes, filename: str, declared: str | None = None) -> str:
    """Best-effort MIME for an upload. Raises UnsupportedFileType if unknown.

    `data` may be only the first few KB for large files -- every signature this
    function inspects lives in the first kilobyte, except the OOXML ZIP
    directory, which needs the whole file.
    """
    hint = normalise(declared) or _from_extension(filename)

    if data.startswith(b"%PDF"):
        return PDF
    if data.startswith(b"PK\x03\x04"):
        sniffed = _sniff_ooxml(data, hint)
        if sniffed:
            return sniffed
        raise UnsupportedFileType("application/zip", filename)
    if data.startswith(_OLE2_SIGNATURE):
        # Legacy Office container -- the signature alone cannot tell doc/xls/ppt
        # apart without parsing the OLE directory, so trust the hint.
        if hint in (DOC, PPT, XLS):
            return hint
        raise UnsupportedFileType("application/x-ole-storage", filename)
    if _looks_like_html(data):
        return HTML

    if _is_text(data):
        # Text-ish: the extension decides csv vs md vs txt, since all three are
        # valid plain text and only the caller knows which was intended.
        if hint in (CSV, MD, TXT, HTML):
            return hint
        return TXT

    raise UnsupportedFileType(hint or "application/octet-stream", filename)
