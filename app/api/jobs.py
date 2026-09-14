"""Job status polling."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, get_session, require_admin
from app.api.schemas import JobStatusResponse
from app.models.ingest_job import IngestJob

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("/{job_id}", response_model=JobStatusResponse)
async def get_job(
    job_id: UUID,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> IngestJob:
    """Status, stage, progress and error for one ingestion job.

    `stage` uses the same vocabulary as `documents.status`, and `checkpoint`
    carries the resume state -- how many chunks have been embedded so far.
    """
    job = (
        await session.execute(select(IngestJob).where(IngestJob.id == job_id))
    ).scalar_one_or_none()
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Job {job_id} not found."
        )
    return job
