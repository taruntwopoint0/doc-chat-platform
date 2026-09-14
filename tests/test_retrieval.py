"""Hybrid search and grounded answering (Milestone 2).

The properties that matter here are safety properties: a search must never
reach outside its workspace, must never surface a document that is not fully
indexed, and an answer must refuse rather than invent.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.implementations.pgvector_store import PgVectorStore
from app.interfaces.types import Chunk as ChunkData
from app.interfaces.types import DocumentStatus, SearchResult
from app.models.document import Document
from app.models.workspace import Workspace
from app.pipeline.answering import (
    NO_ANSWER,
    answer_question,
    build_sources,
    retrieve,
)
from tests.conftest import FakeEmbedder, FakeLLM

DIMS = 768


def vec(seed: float) -> list[float]:
    """A vector pointing in a direction determined by `seed`."""
    return [seed] * DIMS


async def make_doc(session, workspace, filename, status=DocumentStatus.READY):
    doc = Document(
        id=uuid4(),
        workspace_id=workspace.id,
        filename=filename,
        mime_type="text/markdown",
        file_hash=uuid4().hex,
        size_bytes=100,
        version_label="v1",
        status=status,
    )
    session.add(doc)
    await session.commit()
    return doc


async def add_chunks(session, workspace, doc, items):
    """items: list of (ordinal, content, vector_seed, tags)."""
    await PgVectorStore(session).upsert(
        [
            ChunkData(
                id=uuid4(),
                document_id=doc.id,
                workspace_id=workspace.id,
                ordinal=o,
                content=content,
                contextual_header=f"{doc.filename} > Section",
                section_path=["Section"],
                tags=tags,
                token_count=20,
                embedding=vec(seed),
            )
            for o, content, seed, tags in items
        ]
    )
    await session.commit()


# --- tenant isolation ----------------------------------------------------


async def test_search_never_crosses_the_workspace_boundary(session, workspace):
    other = Workspace(id=uuid4(), name="Other", slug=f"o-{uuid4().hex[:8]}",
                      config={}, profile={})
    session.add(other)
    await session.commit()

    mine = await make_doc(session, workspace, "mine.md")
    theirs = await make_doc(session, other, "theirs.md")
    await add_chunks(session, workspace, mine,
                     [(0, "escalate a P1 incident to the duty manager", 0.10, {})])
    await add_chunks(session, other, theirs,
                     [(0, "escalate a P1 incident to the duty manager", 0.10, {})])

    hits = await PgVectorStore().hybrid_search(
        workspace_id=workspace.id, query="escalate a P1 incident",
        query_vector=vec(0.10), filters={}, limit=10,
    )
    assert hits, "expected a hit in the caller's own workspace"
    assert {h.metadata["filename"] for h in hits} == {"mine.md"}


async def test_documents_that_are_not_ready_are_invisible(session, workspace):
    ready = await make_doc(session, workspace, "ready.md")
    part = await make_doc(session, workspace, "indexing.md", DocumentStatus.EMBEDDING)
    await add_chunks(session, workspace, ready, [(0, "alpha beta gamma", 0.2, {})])
    await add_chunks(session, workspace, part, [(0, "alpha beta gamma", 0.2, {})])

    hits = await PgVectorStore().hybrid_search(
        workspace_id=workspace.id, query="alpha beta gamma",
        query_vector=vec(0.2), filters={}, limit=10,
    )
    assert {h.metadata["filename"] for h in hits} == {"ready.md"}


# --- fusion --------------------------------------------------------------


async def test_a_chunk_found_by_both_halves_outranks_one_found_by_either(
    session, workspace
):
    doc = await make_doc(session, workspace, "doc.md")
    await add_chunks(
        session, workspace, doc,
        [
            # Matches the query text and sits nearest the query vector.
            (0, "quarterly escalation policy for priority incidents", 0.50, {}),
            # Near in vector space, but shares no query words.
            (1, "unrelated wording entirely", 0.51, {}),
            # Shares words, but far away in vector space.
            (2, "quarterly escalation policy", 0.99, {}),
        ],
    )
    hits = await PgVectorStore().hybrid_search(
        workspace_id=workspace.id,
        query="quarterly escalation policy",
        query_vector=vec(0.50),
        filters={}, limit=10,
    )
    assert hits[0].content.startswith("quarterly escalation policy for priority")
    top = hits[0]
    assert top.metadata["vector_rank"] is not None
    assert top.metadata["text_rank"] is not None


async def test_full_text_finds_an_exact_token_the_vector_half_may_miss(
    session, workspace
):
    """Identifiers are exactly what embeddings are bad at."""
    doc = await make_doc(session, workspace, "codes.md")
    await add_chunks(
        session, workspace, doc,
        [
            (0, "Incident code INC0042317 covers mailbox migration failures", 0.9, {}),
            (1, "General guidance about mailboxes and migrations", 0.1, {}),
        ],
    )
    hits = await PgVectorStore().hybrid_search(
        workspace_id=workspace.id,
        query="INC0042317",
        query_vector=vec(0.1),  # deliberately points at the *wrong* chunk
        filters={}, limit=5,
    )
    assert "INC0042317" in hits[0].content


async def test_results_come_back_ranked_and_capped(session, workspace):
    doc = await make_doc(session, workspace, "many.md")
    await add_chunks(
        session, workspace, doc,
        [(i, f"passage number {i} about service management", 0.1 + i / 100, {})
         for i in range(15)],
    )
    hits = await PgVectorStore().hybrid_search(
        workspace_id=workspace.id, query="service management",
        query_vector=vec(0.1), filters={}, limit=5,
    )
    assert len(hits) == 5
    assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)


async def test_tag_filters_narrow_the_search(session, workspace):
    doc = await make_doc(session, workspace, "tagged.md")
    await add_chunks(
        session, workspace, doc,
        [
            (0, "holiday entitlement rules", 0.3, {"document_type": "policy"}),
            (1, "holiday entitlement rules", 0.3, {"document_type": "runbook"}),
        ],
    )
    hits = await PgVectorStore().hybrid_search(
        workspace_id=workspace.id, query="holiday entitlement",
        query_vector=vec(0.3), filters={"document_type": "policy"}, limit=10,
    )
    assert len(hits) == 1
    assert hits[0].metadata["tags"]["document_type"] == "policy"


async def test_an_empty_workspace_returns_nothing(session, workspace):
    hits = await PgVectorStore().hybrid_search(
        workspace_id=workspace.id, query="anything at all",
        query_vector=vec(0.4), filters={}, limit=10,
    )
    assert hits == []


async def test_retrieve_embeds_the_question_as_a_query(session, workspace):
    doc = await make_doc(session, workspace, "q.md")
    await add_chunks(session, workspace, doc, [(0, "service desk hours", 0.5, {})])

    embedder = FakeEmbedder()
    question = "when is the service desk open?"
    await retrieve(PgVectorStore(), embedder, workspace.id, question)
    assert embedder.batches == [[question]]


# --- answering -----------------------------------------------------------


def result(number: int, filename: str, content: str) -> SearchResult:
    return SearchResult(
        chunk_id=uuid4(), document_id=uuid4(), content=content,
        contextual_header=None, section_path=["Section"], score=1.0 / number,
        metadata={"filename": filename},
    )


@pytest.mark.no_db
def test_sources_are_numbered_for_citation():
    text = build_sources([result(1, "a.md", "alpha"), result(2, "b.md", "beta")])
    assert "[1] a.md > Section" in text
    assert "[2] b.md > Section" in text


@pytest.mark.no_db
async def test_no_results_is_an_immediate_refusal_without_calling_the_llm():
    llm = FakeLLM()
    answer = await answer_question(llm, "anything", [])
    assert answer.refused is True
    assert llm.calls == [], "the model was asked despite having no sources"


@pytest.mark.no_db
async def test_the_no_answer_sentinel_becomes_a_refusal():
    answer = await answer_question(
        FakeLLM(NO_ANSWER), "what is the capital of France?",
        [result(1, "a.md", "unrelated text")],
    )
    assert answer.refused is True
    assert NO_ANSWER not in answer.text, "the sentinel leaked into the user-facing text"


@pytest.mark.no_db
async def test_citations_are_resolved_back_to_their_chunks():
    results = [
        result(1, "runbook.docx", "Page the on-call engineer."),
        result(2, "access.html", "The line manager approves."),
        result(3, "unused.md", "Never cited."),
    ]
    answer = await answer_question(
        FakeLLM("Page the on-call engineer [1]. The line manager approves [2]."),
        "how does escalation work?", results,
    )
    assert answer.refused is False
    assert [c.number for c in answer.citations] == [1, 2]
    assert [c.filename for c in answer.citations] == ["runbook.docx", "access.html"]
    assert answer.considered == 3


@pytest.mark.no_db
async def test_out_of_range_citations_are_ignored():
    """A model that cites [9] against three sources must not crash the request."""
    answer = await answer_question(
        FakeLLM("Something something [9]."), "q", [result(1, "a.md", "x")]
    )
    assert answer.citations == []


@pytest.mark.no_db
async def test_a_repeated_citation_is_listed_once():
    answer = await answer_question(
        FakeLLM("First [1]. Second [1]. Third [1]."), "q", [result(1, "a.md", "x")]
    )
    assert [c.number for c in answer.citations] == [1]
