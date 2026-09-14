"""Corpus profiling and the accumulated workspace vocabulary.

The point of these tests is that nothing in the pipeline knows what domain it
is looking at. The same code must build an ITSM vocabulary from ITSM documents
and an HR vocabulary from HR documents, with no code path that favours either.
"""

from __future__ import annotations

import json

import pytest

from app.interfaces.types import Block, BlockKind, ChunkDraft, DocumentProfile
from app.pipeline.contextualise import build_header, contextualise
from app.pipeline.profiling import (
    build_prompt,
    chunk_tags,
    heading_tree,
    merge_workspace_profile,
    profile_document,
    rebuild_workspace_profile,
    section_digest,
)
from tests.conftest import FakeLLM

pytestmark = pytest.mark.no_db


def doc_blocks() -> list[Block]:
    return [
        Block(kind=BlockKind.HEADING, text="Incident Management",
              section_path=["Incident Management"], level=1),
        Block(kind=BlockKind.TEXT, text="How incidents are raised and tracked.",
              section_path=["Incident Management"]),
        Block(kind=BlockKind.HEADING, text="Escalation",
              section_path=["Incident Management", "Escalation"], level=2),
        Block(kind=BlockKind.TEXT, text="Escalate a P1 to the duty manager.",
              section_path=["Incident Management", "Escalation"]),
    ]


# --- prompt construction -------------------------------------------------


def test_heading_tree_preserves_nesting():
    tree = heading_tree(doc_blocks())
    assert "- Incident Management" in tree
    assert "  - Escalation" in tree


def test_section_digest_is_capped_per_section():
    blocks = [
        Block(kind=BlockKind.TEXT, text="x" * 5000, section_path=["A"]),
    ]
    digest = section_digest(blocks, chars_per_section=100)
    assert "## A" in digest
    assert len(digest) < 300


def test_prompt_contains_the_tree_the_digest_and_the_schema():
    prompt = build_prompt(doc_blocks(), "runbook.docx", 2000)
    assert "runbook.docx" in prompt
    assert "Incident Management" in prompt
    assert "Escalate a P1 to the duty manager." in prompt
    for field in ("document_type", "summary", "topics", "entities"):
        assert field in prompt


def test_prompt_carries_no_domain_vocabulary_of_its_own():
    """The prompt must not suggest answers -- that is what would bake a domain in."""
    prompt = build_prompt([], "empty.txt", 2000).lower()
    for leaked in ("itsm", "incident", "ticket", "sla", "hr", "contract", "invoice"):
        assert leaked not in prompt


# --- profile extraction --------------------------------------------------


async def test_profile_is_parsed_from_the_llm_response():
    llm = FakeLLM(
        '{"document_type": "procedure guide", "summary": "Covers incidents. Two.",'
        ' "topics": ["escalation", "priority"], "entities": ["ServiceNow"]}'
    )
    profile = await profile_document(llm, doc_blocks(), "runbook.docx", 2000)

    assert profile.document_type == "procedure guide"
    assert profile.topics == ["escalation", "priority"]
    assert profile.entities == ["ServiceNow"]


async def test_a_fenced_json_response_is_still_parsed():
    llm = FakeLLM('```json\n{"document_type": "handbook", "topics": ["leave"]}\n```')
    profile = await profile_document(llm, doc_blocks(), "hr.docx", 2000)
    assert profile.document_type == "handbook"
    assert profile.topics == ["leave"]


async def test_a_failed_profile_does_not_stop_the_document_indexing():
    class Broken(FakeLLM):
        async def complete(self, *args, **kwargs):
            raise RuntimeError("provider down")

    profile = await profile_document(Broken(), doc_blocks(), "x.md", 2000)
    assert profile == DocumentProfile()


async def test_unparseable_json_yields_an_empty_profile():
    llm = FakeLLM("not json at all")
    profile = await profile_document(llm, doc_blocks(), "x.md", 2000)
    assert profile.topics == []


async def test_topics_are_deduplicated_and_capped():
    topics = ["dup"] * 5 + [f"t{i}" for i in range(30)]
    llm = FakeLLM(json.dumps({"topics": topics}))
    profile = await profile_document(llm, doc_blocks(), "x.md", 2000)
    assert len(profile.topics) <= 12
    assert profile.topics.count("dup") == 1


# --- workspace vocabulary ------------------------------------------------


def test_workspace_vocabulary_accumulates_across_documents():
    a = DocumentProfile(document_type="procedure guide", topics=["escalation", "sla"],
                        entities=["ServiceNow"])
    b = DocumentProfile(document_type="procedure guide", topics=["escalation"],
                        entities=["Jira"])

    profile = merge_workspace_profile(merge_workspace_profile({}, a), b)

    assert profile["topics"] == {"escalation": 2, "sla": 1}
    assert profile["entities"] == {"ServiceNow": 1, "Jira": 1}
    assert profile["document_types"] == {"procedure guide": 2}
    assert profile["document_count"] == 2


def test_the_same_code_builds_an_unrelated_domain_vocabulary():
    """No branch anywhere prefers one domain over another."""
    hr = [
        DocumentProfile(document_type="handbook", topics=["parental leave"],
                        entities=["Workday"]),
        DocumentProfile(document_type="policy", topics=["parental leave", "expenses"],
                        entities=["Workday"]),
    ]
    profile = rebuild_workspace_profile([p.to_dict() for p in hr])
    assert profile["topics"] == {"parental leave": 2, "expenses": 1}
    assert profile["entities"] == {"Workday": 2}
    assert profile["document_count"] == 2


def test_rebuilding_drops_documents_that_are_gone():
    kept = DocumentProfile(topics=["kept"])
    removed = DocumentProfile(topics=["removed"])
    full = merge_workspace_profile(merge_workspace_profile({}, kept), removed)
    assert "removed" in full["topics"]

    rebuilt = rebuild_workspace_profile([kept.to_dict()])
    assert rebuilt["topics"] == {"kept": 1}
    assert rebuilt["document_count"] == 1


def test_an_empty_profile_still_counts_the_document():
    profile = merge_workspace_profile({}, DocumentProfile())
    assert profile["document_count"] == 1
    assert profile["topics"] == {}


def test_chunk_tags_carry_the_document_profile():
    profile = DocumentProfile(document_type="guide", topics=["a"], entities=["B"])
    assert chunk_tags(profile) == {
        "document_type": "guide",
        "topics": ["a"],
        "entities": ["B"],
    }


# --- contextual headers --------------------------------------------------


def test_header_is_document_then_section_then_description():
    header = build_header("runbook.docx", ["Incidents", "Escalation"], "How to escalate")
    assert header == "runbook.docx > Incidents > Escalation - How to escalate"


def test_header_without_a_description_is_still_useful():
    assert build_header("runbook.docx", ["Incidents"], "") == "runbook.docx > Incidents"


async def test_contextualise_fills_every_header_and_leaves_content_alone():
    drafts = [
        ChunkDraft(ordinal=0, content="Body one.", section_path=["A"]),
        ChunkDraft(ordinal=1, content="Body two.", section_path=["A"]),
        ChunkDraft(ordinal=2, content="Body three.", section_path=["B"]),
    ]
    await contextualise(drafts, "manual.pdf", DocumentProfile(),
                        FakeLLM('{"s0": "About A", "s1": "About B"}'))

    assert all(d.contextual_header for d in drafts)
    assert drafts[0].content == "Body one.", "content must stay clean for the UI"
    assert drafts[0].contextual_header.startswith("manual.pdf > A")
    assert drafts[2].contextual_header.startswith("manual.pdf > B")


async def test_headers_still_work_with_no_llm_available():
    drafts = [ChunkDraft(ordinal=0, content="Body.", section_path=["A", "B"])]
    await contextualise(drafts, "manual.pdf", DocumentProfile(), llm=None)
    assert drafts[0].contextual_header == "manual.pdf > A > B"


async def test_one_llm_call_covers_every_section_by_default():
    """Per-section is the default because per-chunk is one call per chunk."""
    llm = FakeLLM('{"s0": "About A"}')
    drafts = [
        ChunkDraft(ordinal=i, content=f"Body {i}.", section_path=["A"])
        for i in range(20)
    ]
    await contextualise(drafts, "manual.pdf", DocumentProfile(), llm)
    assert len(llm.calls) == 1


async def test_per_chunk_mode_asks_about_each_chunk():
    llm = FakeLLM('{"description": "A line"}')
    drafts = [
        ChunkDraft(ordinal=i, content=f"Body {i}.", section_path=["A"]) for i in range(3)
    ]
    await contextualise(drafts, "manual.pdf", DocumentProfile(), llm, per_chunk=True)
    assert len(llm.calls) == 3
    assert drafts[0].contextual_header.endswith("A line")
