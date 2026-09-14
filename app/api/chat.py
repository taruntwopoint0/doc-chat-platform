"""Chat and search routes (Milestone 2)."""

from __future__ import annotations

import logging
from math import ceil
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, get_session, require_user, require_workspace
from app.api.schemas import (
    ChatMessageOut,
    ChatRequest,
    ChatResponse,
    CitationOut,
    ClearedHistory,
    SearchHit,
    SearchRequest,
)
from app.config import Settings, get_settings
from app.db import session_scope
from app.implementations.retry import RateLimited, interactive
from app.models.chat import ChatMessage
from app.models.document import Document
from app.models.workspace import Workspace
from app.pipeline import conversation
from app.pipeline.answering import answer_question, retrieve
from app.registry import get_embedder, get_llm, get_vector_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/workspaces", tags=["chat"])


_WORKING = ("pending", "parsing", "profiling", "chunking", "embedding")


async def _corpus_state(workspace_id) -> tuple[list[str], int]:
    """(filenames of ready documents, count still being processed)."""
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Document.filename, Document.status)
                .where(Document.workspace_id == workspace_id)
                .order_by(Document.created_at)
            )
        ).all()
    ready = [name for name, state in rows if state == "ready"]
    processing = sum(1 for _, state in rows if state in _WORKING)
    return ready, processing


def _rate_limited(exc: RateLimited, workspace_id) -> HTTPException:
    logger.warning("Provider rate limit hit in workspace %s", workspace_id)
    headers = {"Retry-After": str(ceil(exc.retry_after))} if exc.retry_after else None
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc), headers=headers
    )


def _top_topics(profile: dict | None, limit: int = 8) -> list[str]:
    """Most frequent topics first -- profiling counts how often each appears."""
    topics = (profile or {}).get("topics") or {}
    return [name for name, _ in sorted(topics.items(), key=lambda kv: -kv[1])][:limit]


async def _save_turns(
    workspace_id, user_id, question: str, answer, citations: list[dict]
) -> None:
    """Persist the exchange so reloading or switching back restores it.

    Skipped when the caller authenticated with the shared admin token: history
    belongs to a person, and a token is not one.
    """
    if user_id is None:
        return
    async with session_scope() as session:
        session.add(
            ChatMessage(
                id=uuid4(), workspace_id=workspace_id, user_id=user_id,
                role="user", content=question, citations=[], refused=False,
            )
        )
        session.add(
            ChatMessage(
                id=uuid4(), workspace_id=workspace_id, user_id=user_id,
                role="assistant", content=answer.text, citations=citations,
                refused=answer.refused,
            )
        )


@router.get("/{workspace_id}/history", response_model=list[ChatMessageOut])
async def history(
    workspace: Workspace = Depends(require_workspace),
    principal: Principal = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> list[ChatMessage]:
    """This user's conversation in this workspace, oldest first."""
    if principal.user_id is None:
        return []
    rows = await session.execute(
        select(ChatMessage)
        .where(
            ChatMessage.workspace_id == workspace.id,
            ChatMessage.user_id == principal.user_id,
        )
        .order_by(ChatMessage.created_at)
    )
    return list(rows.scalars().all())


@router.delete("/{workspace_id}/history", response_model=ClearedHistory)
async def clear_history(
    workspace: Workspace = Depends(require_workspace),
    principal: Principal = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> ClearedHistory:
    """Clear this user's conversation in this workspace.

    Deletes only the conversation. Documents, chunks and the index are
    untouched -- clearing what you asked is not the same as deleting the
    corpus, and conflating them would be an unpleasant surprise.
    """
    if principal.user_id is None:
        return ClearedHistory(workspace_id=workspace.id, deleted=0)
    result = await session.execute(
        delete(ChatMessage)
        .where(
            ChatMessage.workspace_id == workspace.id,
            ChatMessage.user_id == principal.user_id,
        )
        .returning(ChatMessage.id)
    )
    deleted = len(result.all())
    await session.commit()
    return ClearedHistory(workspace_id=workspace.id, deleted=deleted)


@router.post("/{workspace_id}/chat", response_model=ChatResponse)
async def chat(
    payload: ChatRequest,
    workspace: Workspace = Depends(require_workspace),
    principal: Principal = Depends(require_user),
    settings: Settings = Depends(get_settings),
) -> ChatResponse:
    """Answer a question from this workspace's documents only.

    Retrieval and answering are both scoped to `workspace_id`, so a question
    can never be answered from another tenant's corpus.
    """
    question = payload.question.strip()
    if not question:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Question is empty."
        )

    kind = conversation.classify(question)
    ready, processing = await _corpus_state(workspace.id)
    # A greeting, or any question asked of an empty workspace, is answered from
    # what the workspace contains. Retrieval would only find nothing and report
    # "the documents do not cover that", which is true and no help at all.
    if kind is not None or not ready:
        answer = conversation.reply(
            kind,
            ready_filenames=ready,
            topics=_top_topics(workspace.profile),
            processing=processing,
        )
        await _save_turns(workspace.id, principal.user_id, question, answer, [])
        return ChatResponse(
            question=question,
            answer=answer.text,
            refused=False,
            considered=0,
            citations=[],
        )

    try:
        # A person is waiting: a quota wait longer than a few seconds should
        # come back as an answerable "busy" rather than a minute-long spinner.
        with interactive():
            results = await retrieve(
                store=get_vector_store(),
                embedder=get_embedder(),
                workspace_id=workspace.id,
                question=question,
                filters=payload.filters or {},
                limit=payload.top_k or settings.retrieval_top_k,
            )
            answer = await answer_question(get_llm(), question, results)
    except RateLimited as exc:
        raise _rate_limited(exc, workspace.id) from exc
    except Exception as exc:
        logger.exception("Chat failed for workspace %s", workspace.id)
        # The provider's raw error carries internal URLs and a kilobyte of JSON;
        # it belongs in the log, not on the screen.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The AI service returned an error. Try again in a moment.",
        ) from exc

    citations = [
        CitationOut(
            number=c.number,
            document_id=c.document_id,
            chunk_id=c.chunk_id,
            filename=c.filename,
            section_path=c.section_path,
            snippet=c.snippet,
            score=round(c.score, 5),
        )
        for c in answer.citations
    ]

    await _save_turns(
        workspace.id,
        principal.user_id,
        question,
        answer,
        [c.model_dump(mode="json") for c in citations],
    )

    return ChatResponse(
        question=question,
        answer=answer.text,
        refused=answer.refused,
        considered=answer.considered,
        citations=citations,
    )


@router.post("/{workspace_id}/search", response_model=list[SearchHit])
async def search(
    payload: SearchRequest,
    workspace: Workspace = Depends(require_workspace),
    settings: Settings = Depends(get_settings),
) -> list[SearchHit]:
    """Raw hybrid search, no LLM. Useful for checking why an answer came out
    the way it did."""
    try:
        with interactive():
            results = await retrieve(
                store=get_vector_store(),
                embedder=get_embedder(),
                workspace_id=workspace.id,
                question=payload.query.strip(),
                filters=payload.filters or {},
                limit=payload.top_k or settings.retrieval_top_k,
            )
    except RateLimited as exc:
        raise _rate_limited(exc, workspace.id) from exc
    return [
        SearchHit(
            chunk_id=r.chunk_id,
            document_id=r.document_id,
            filename=str(r.metadata.get("filename", "")),
            section_path=r.section_path,
            content=r.content,
            score=round(r.score, 5),
            vector_rank=r.metadata.get("vector_rank"),
            text_rank=r.metadata.get("text_rank"),
        )
        for r in results
    ]
