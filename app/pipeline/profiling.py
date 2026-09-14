"""Derive a description of each document, and accumulate it per workspace.

This is what keeps the platform domain-agnostic. Nothing here names a domain:
the tag vocabulary is whatever the LLM reads out of the corpus, so an ITSM
workspace grows ITSM tags and an HR workspace grows HR tags from identical code.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from app.implementations.gemini_llm import parse_json_object
from app.interfaces.llm import LLM
from app.interfaces.types import Block, BlockKind, DocumentProfile

logger = logging.getLogger(__name__)

SYSTEM = (
    "You describe documents for a retrieval system. You never invent content "
    "that is not in the text you are given. You reply with JSON only."
)

_TEMPLATE = """Describe this document.

Filename: {filename}

Heading tree:
{tree}

Section excerpts:
{digest}

Reply with a JSON object and nothing else:
{{
  "document_type": "short label describing what kind of document this is",
  "summary": "two sentences describing what the document covers",
  "topics": ["subject keywords a reader might search for"],
  "entities": ["specific systems, forms, teams, or products named in the text"]
}}

Derive every value from the text above. Use the document's own vocabulary.
Give at most 12 topics and at most 12 entities. If the text does not support a
field, use an empty list or an empty string."""

#: Cap the prompt so a 500-page document does not produce a 400k-character call.
_MAX_SECTIONS = 40
_MAX_TREE_LINES = 120


def heading_tree(blocks: list[Block]) -> str:
    lines: list[str] = []
    for block in blocks:
        if block.kind is not BlockKind.HEADING:
            continue
        level = block.level or len(block.section_path) or 1
        lines.append(f"{'  ' * (level - 1)}- {block.text}")
        if len(lines) >= _MAX_TREE_LINES:
            lines.append("  ... (tree truncated)")
            break
    return "\n".join(lines) if lines else "(no headings)"


def section_digest(blocks: list[Block], chars_per_section: int) -> str:
    """First `chars_per_section` characters of each section's body text."""
    sections: dict[str, list[str]] = {}
    order: list[str] = []
    for block in blocks:
        if block.kind is BlockKind.HEADING:
            continue
        key = " > ".join(block.section_path) or "(document root)"
        if key not in sections:
            sections[key] = []
            order.append(key)
        body = sections[key]
        if sum(len(p) for p in body) < chars_per_section:
            body.append(block.text)

    parts: list[str] = []
    for key in order[:_MAX_SECTIONS]:
        text = " ".join(sections[key])[:chars_per_section].strip()
        if text:
            parts.append(f"## {key}\n{text}")
    if len(order) > _MAX_SECTIONS:
        parts.append(f"... ({len(order) - _MAX_SECTIONS} further sections omitted)")
    return "\n\n".join(parts) if parts else "(no body text)"


def build_prompt(blocks: list[Block], filename: str, chars_per_section: int) -> str:
    return _TEMPLATE.format(
        filename=filename,
        tree=heading_tree(blocks),
        digest=section_digest(blocks, chars_per_section),
    )


def _string_list(value: object, limit: int = 12) -> list[str]:
    if not isinstance(value, list):
        return []
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item).strip()
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            cleaned.append(text)
        if len(cleaned) >= limit:
            break
    return cleaned


async def profile_document(
    llm: LLM, blocks: list[Block], filename: str, chars_per_section: int
) -> DocumentProfile:
    """Ask the LLM to describe the document.

    A failure here is not fatal: an unprofiled document still indexes, it just
    contributes no tags. Refusing to index it would be a worse trade.
    """
    prompt = build_prompt(blocks, filename, chars_per_section)
    try:
        raw = await llm.complete(prompt, system=SYSTEM, json_mode=True, temperature=0.1)
    except Exception as exc:
        logger.warning("Profiling failed for %s: %s", filename, exc)
        return DocumentProfile()

    data = parse_json_object(raw)
    return DocumentProfile(
        document_type=str(data.get("document_type") or "").strip()[:120],
        summary=str(data.get("summary") or "").strip()[:1000],
        topics=_string_list(data.get("topics")),
        entities=_string_list(data.get("entities")),
    )


def _bump(counts: dict, values: list[str]) -> dict:
    merged = dict(counts)
    for value in values:
        merged[value] = int(merged.get(value, 0)) + 1
    return merged


def merge_workspace_profile(existing: dict | None, profile: DocumentProfile) -> dict:
    """Fold a document's profile into the workspace's accumulated vocabulary.

    Values are counted rather than collected, so the workspace knows which
    topics are common and which appear once -- useful for query expansion and
    filter suggestions in Milestone 2.
    """
    current = dict(existing or {})
    return {
        "topics": _bump(current.get("topics") or {}, profile.topics),
        "entities": _bump(current.get("entities") or {}, profile.entities),
        "document_types": _bump(
            current.get("document_types") or {},
            [profile.document_type] if profile.document_type else [],
        ),
        "document_count": int(current.get("document_count") or 0) + 1,
        "updated_at": datetime.now(UTC).isoformat(),
    }


def rebuild_workspace_profile(profiles: list[dict]) -> dict:
    """Recompute a workspace profile from scratch.

    Needed after a delete or a reindex, where incrementing counts would leave
    the vocabulary describing documents that are no longer there.
    """
    result: dict = {}
    for raw in profiles:
        result = merge_workspace_profile(
            result,
            DocumentProfile(
                document_type=str(raw.get("document_type") or ""),
                summary=str(raw.get("summary") or ""),
                topics=_string_list(raw.get("topics")),
                entities=_string_list(raw.get("entities")),
            ),
        )
    return result


def chunk_tags(profile: DocumentProfile) -> dict:
    """Tags copied onto every chunk of a document, for metadata filtering."""
    return {
        "document_type": profile.document_type,
        "topics": profile.topics,
        "entities": profile.entities,
    }
