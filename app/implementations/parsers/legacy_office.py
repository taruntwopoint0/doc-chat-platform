"""Legacy binary Office formats (DOC / PPT / XLS).

These are converted to their modern equivalent with headless LibreOffice, then
handed straight back to the registry, so the conversion is the only extra step
and DOCX/PPTX/XLSX handling stays in one place.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path

from app.config import get_settings
from app.implementations.parsers import mime as mimes
from app.interfaces.parser import DocumentParser
from app.interfaces.types import ParsedDocument

logger = logging.getLogger(__name__)

#: legacy MIME -> (soffice target filter, resulting extension, resulting MIME)
_CONVERSIONS = {
    mimes.DOC: ("docx", ".docx", mimes.DOCX),
    mimes.PPT: ("pptx", ".pptx", mimes.PPTX),
    mimes.XLS: ("xlsx", ".xlsx", mimes.XLSX),
}


class LegacyOfficeConversionError(RuntimeError):
    pass


class LegacyOfficeParser(DocumentParser):
    mime_types = tuple(_CONVERSIONS)

    def __init__(self, registry) -> None:  # noqa: ANN001 - avoids a circular import
        self._registry = registry

    async def parse(self, file_path: str, mime_type: str) -> ParsedDocument:
        normalised = mimes.normalise(mime_type) or mime_type
        target_filter, extension, target_mime = _CONVERSIONS[normalised]

        with tempfile.TemporaryDirectory(prefix="soffice-") as workdir:
            try:
                converted = await self._convert(
                    file_path, workdir, target_filter, extension
                )
            except LegacyOfficeConversionError:
                fallback = await self._native_fallback(file_path, normalised)
                if fallback is not None:
                    return fallback
                raise

            parser = self._registry.get(target_mime)
            parsed = await parser.parse(str(converted), target_mime)

        parsed.metadata["converted_from"] = normalised
        parsed.metadata["converted_via"] = "soffice"
        return parsed

    async def _convert(
        self, file_path: str, outdir: str, target_filter: str, extension: str
    ) -> Path:
        settings = get_settings()
        binary = shutil.which(settings.soffice_binary) or settings.soffice_binary
        cmd = [
            binary,
            "--headless",
            "--norestore",
            "--convert-to",
            target_filter,
            "--outdir",
            outdir,
            file_path,
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, NotADirectoryError, OSError) as exc:
            raise LegacyOfficeConversionError(
                f"LibreOffice ({settings.soffice_binary}) is not available: {exc}. "
                "Install it, or set SOFFICE_BINARY to its path, to ingest "
                ".doc/.ppt/.xls files."
            ) from exc

        try:
            _, stderr = await asyncio.wait_for(
                process.communicate(), timeout=settings.soffice_timeout_seconds
            )
        except TimeoutError as exc:
            process.kill()
            raise LegacyOfficeConversionError(
                f"LibreOffice timed out after {settings.soffice_timeout_seconds}s"
            ) from exc

        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise LegacyOfficeConversionError(
                f"LibreOffice exited {process.returncode}: {detail[:500]}"
            )

        # soffice names the output after the input stem, so find it rather than
        # assuming the path.
        produced = sorted(Path(outdir).glob(f"*{extension}"))
        if not produced:
            raise LegacyOfficeConversionError(
                f"LibreOffice produced no {extension} file for {Path(file_path).name}"
            )
        return produced[0]

    async def _native_fallback(
        self, file_path: str, normalised: str
    ) -> ParsedDocument | None:
        """XLS can be read directly by xlrd, so a missing LibreOffice is not fatal.

        DOC and PPT have no pure-Python reader here and fail with the
        conversion error instead.
        """
        if normalised != mimes.XLS:
            return None
        logger.warning("LibreOffice unavailable; reading %s with xlrd", file_path)
        from app.implementations.parsers.tabular_parser import TabularParser

        parsed = await TabularParser().parse(file_path, mimes.XLS)
        parsed.metadata["converted_via"] = "xlrd-fallback"
        return parsed
