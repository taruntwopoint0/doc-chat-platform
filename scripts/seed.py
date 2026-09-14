"""Create a workspace and ingest a folder of documents, without the UI.

    python scripts/seed.py --folder ./sample-docs --slug itsm --name "ITSM"

Runs the same pipeline the API and worker do -- intake, then the ingest job --
but inline and one document at a time, so failures surface immediately instead
of in a job row. Use it to try a corpus end to end before there is a front end.

    --no-embed   skip the embedding stage (no Gemini key, no API cost). Chunks
                 are written with NULL vectors; a later run fills them in.
    --reset      delete an existing workspace with the same slug first.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from uuid import uuid4

# Allow `python scripts/seed.py` from the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import delete, func, select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import dispose_engine, session_scope  # noqa: E402
from app.implementations.parsers import mime as mimes  # noqa: E402
from app.models.chunk import Chunk  # noqa: E402
from app.models.document import Document  # noqa: E402
from app.models.workspace import Workspace  # noqa: E402
from app.pipeline import ingest  # noqa: E402
from app.pipeline.intake import intake_upload  # noqa: E402
from app.registry import validate_wiring  # noqa: E402

logger = logging.getLogger("seed")


async def _get_or_create_workspace(slug: str, name: str, reset: bool) -> Workspace:
    async with session_scope() as session:
        existing = (
            await session.execute(select(Workspace).where(Workspace.slug == slug))
        ).scalar_one_or_none()

        if existing is not None and reset:
            logger.info("Deleting existing workspace '%s'", slug)
            await session.execute(delete(Workspace).where(Workspace.id == existing.id))
            existing = None

        if existing is not None:
            logger.info("Reusing workspace '%s' (%s)", slug, existing.id)
            return existing

        workspace = Workspace(
            id=uuid4(), name=name, slug=slug, config={}, profile={}
        )
        session.add(workspace)
        await session.flush()
        logger.info("Created workspace '%s' (%s)", slug, workspace.id)
        return workspace


def _candidate_files(folder: Path) -> list[Path]:
    files = [
        p
        for p in sorted(folder.rglob("*"))
        if p.is_file() and not p.name.startswith(".")
    ]
    if not files:
        raise SystemExit(f"No files found under {folder}")
    return files


async def _ingest_one(workspace: Workspace, path: Path, embed: bool) -> str:
    data = path.read_bytes()
    settings = get_settings()

    async with session_scope() as session:
        try:
            result = await intake_upload(
                session,
                workspace_id=workspace.id,
                filename=path.name,
                data=data,
                declared_mime=None,
                max_upload_bytes=settings.max_upload_bytes,
            )
        except mimes.UnsupportedFileType as exc:
            return f"skipped ({exc.detected})"
        except ValueError as exc:
            return f"rejected ({exc})"

        if result.deduplicated:
            return "already indexed"
        job = result.job
        document_id = result.document.id

    # Re-read the job outside the intake transaction, the way the worker does.
    async with session_scope() as session:
        from app.models.ingest_job import IngestJob

        job = (
            await session.execute(select(IngestJob).where(IngestJob.id == job.id))
        ).scalar_one()
        session.expunge(job)

    await ingest.run_job(job, embed=embed)

    async with session_scope() as session:
        chunks = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(Chunk)
                    .where(Chunk.document_id == document_id)
                )
            ).scalar_one()
        )
    return f"{chunks} chunks"


async def _report(workspace_id) -> None:
    async with session_scope() as session:
        workspace = (
            await session.execute(
                select(Workspace).where(Workspace.id == workspace_id)
            )
        ).scalar_one()
        rows = (
            await session.execute(
                select(Document.status, func.count())
                .where(Document.workspace_id == workspace_id)
                .group_by(Document.status)
            )
        ).all()
        chunks = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(Chunk)
                    .where(Chunk.workspace_id == workspace_id)
                )
            ).scalar_one()
        )

    print(f"\nWorkspace {workspace.slug} ({workspace.id})")
    print(f"  documents : {dict((str(s), int(n)) for s, n in rows)}")
    print(f"  chunks    : {chunks}")
    profile = workspace.profile or {}
    print(f"  topics    : {profile.get('topics', {})}")
    print(f"  entities  : {profile.get('entities', {})}")
    print(f"  types     : {profile.get('document_types', {})}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", required=True, type=Path)
    parser.add_argument("--slug", default="sample")
    parser.add_argument("--name", default="Sample workspace")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument(
        "--no-embed",
        action="store_true",
        help="parse, profile and chunk, but do not call the embedding API",
    )
    args = parser.parse_args()

    logging.basicConfig(level="INFO", format="%(levelname)-8s %(name)s: %(message)s")
    if not args.no_embed:
        validate_wiring()

    folder = args.folder.resolve()
    if not folder.is_dir():
        raise SystemExit(f"{folder} is not a directory")

    workspace = await _get_or_create_workspace(args.slug, args.name, args.reset)
    files = _candidate_files(folder)
    print(f"Ingesting {len(files)} file(s) from {folder}\n")

    for path in files:
        label = path.relative_to(folder)
        try:
            outcome = await _ingest_one(workspace, path, embed=not args.no_embed)
        except Exception as exc:
            outcome = f"FAILED: {type(exc).__name__}: {exc}"
        print(f"  {str(label):50s} {outcome}")

    await _report(workspace.id)
    await dispose_engine()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
