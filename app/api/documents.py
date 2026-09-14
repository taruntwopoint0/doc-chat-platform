"""Document upload, listing and deletion."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    Principal,
    get_session,
    require_admin,
    require_workspace,
    require_workspace_admin,
)
from app.api.schemas import DeleteResult, DocumentListItem, UploadAccepted
from app.config import Settings, get_settings
from app.implementations.parsers.mime import UnsupportedFileType
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.workspace import Workspace
from app.pipeline.intake import UploadTooLarge, intake_upload
from app.pipeline.profiling import rebuild_workspace_profile
from app.registry import get_file_store, get_vector_store

router = APIRouter(prefix="/api", tags=["documents"])


@router.post(
    "/workspaces/{workspace_id}/documents",
    response_model=UploadAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_document(
    file: UploadFile = File(...),
    workspace: Workspace = Depends(require_workspace_admin),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> UploadAccepted:
    """Accept a file and return a job id. Parsing happens in the worker.

    The whole file is read into memory to hash it and hand it to the file
    store. MAX_UPLOAD_MB is what bounds that; on a 2 GB instance keep it modest.
    """
    data = await file.read()
    try:
        result = await intake_upload(
            session,
            workspace_id=workspace.id,
            filename=file.filename or "upload",
            data=data,
            declared_mime=file.content_type,
            max_upload_bytes=settings.max_upload_bytes,
        )
    except UnsupportedFileType as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc)
        ) from exc
    except UploadTooLarge as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=str(exc)
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    # The worker must be able to see the job, and the caller must be able to
    # poll it, the moment the job id is returned.
    await session.commit()

    if result.deduplicated:
        message = (
            "Identical content is already indexed; no new work was queued. "
            "Upload a changed file to create a new version."
        )
    elif result.superseded:
        message = (
            f"Queued as {result.document.version_label}. The previous version's "
            "chunks were removed."
        )
    else:
        message = "Queued for ingestion."

    return UploadAccepted(
        document_id=result.document.id,
        job_id=result.job.id if result.job else None,
        status=result.document.status,
        version_label=result.document.version_label,
        deduplicated=result.deduplicated,
        superseded=result.superseded,
        message=message,
    )


@router.get(
    "/workspaces/{workspace_id}/documents", response_model=list[DocumentListItem]
)
async def list_documents(
    workspace: Workspace = Depends(require_workspace),
    session: AsyncSession = Depends(get_session),
) -> list[DocumentListItem]:
    chunk_counts = (
        select(Chunk.document_id, func.count().label("n"))
        .where(Chunk.workspace_id == workspace.id)
        .group_by(Chunk.document_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(Document, func.coalesce(chunk_counts.c.n, 0))
            .outerjoin(chunk_counts, chunk_counts.c.document_id == Document.id)
            .where(Document.workspace_id == workspace.id)
            .order_by(Document.created_at.desc())
        )
    ).all()
    items = []
    for document, count in rows:
        item = DocumentListItem.model_validate(document)
        item.chunk_count = int(count)
        items.append(item)
    return items


@router.delete("/documents/{document_id}", response_model=DeleteResult)
async def delete_document(
    document_id: UUID,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> DeleteResult:
    """Delete a document, its chunks and its stored original."""
    document = (
        await session.execute(select(Document).where(Document.id == document_id))
    ).scalar_one_or_none()
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Document {document_id} not found.",
        )

    workspace_id = document.workspace_id
    chunk_count = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Chunk)
                .where(
                    Chunk.workspace_id == workspace_id,
                    Chunk.document_id == document_id,
                )
            )
        ).scalar_one()
    )

    await get_vector_store(session).delete_by_document(document_id, workspace_id)
    await get_file_store(session).delete(
        workspace_id, document.storage_key or str(document_id)
    )
    # Queued jobs for this document cascade away with the row.
    await session.delete(document)
    await session.flush()

    # The workspace vocabulary counts occurrences, so removing a document means
    # recomputing it -- decrementing would drift as documents are re-versioned.
    workspace = (
        await session.execute(
            select(Workspace).where(Workspace.id == workspace_id).with_for_update()
        )
    ).scalar_one()
    remaining = (
        (
            await session.execute(
                select(Document.profile).where(Document.workspace_id == workspace_id)
            )
        )
        .scalars()
        .all()
    )
    workspace.profile = rebuild_workspace_profile([p for p in remaining if p])
    await session.commit()

    return DeleteResult(
        document_id=document_id,
        chunks_deleted=chunk_count,
        message="Document, chunks and stored original deleted.",
    )
