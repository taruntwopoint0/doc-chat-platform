"""Prepend a context header to each chunk before it is embedded.

Format: `{document name} > {section path} - {one-line description}`.

Stored in `chunks.contextual_header`, separate from `chunks.content`, so the UI
can show clean text while the embedding sees the enriched version. A chunk
pulled out of a 300-page manual otherwise loses every clue about where it came
from, which is what makes mid-document chunks retrieve badly.

Descriptions are generated per section, not per chunk: one cheap LLM call
describes every section of a document at once. Chunks within a section share
that description. `CONTEXT_DESCRIPTIONS=chunk` switches to a call per chunk --
sharper, but one request per chunk across an unbounded corpus.
"""

from __future__ import annotations

import json
import logging

from app.implementations.gemini_llm import parse_json_object
from app.interfaces.llm import LLM
from app.interfaces.types import ChunkDraft, DocumentProfile

logger = logging.getLogger(__name__)

SYSTEM = (
    "You write one-line descriptions of document sections for a retrieval "
    "system. Each description says what the section covers, in at most 15 "
    "words. You never invent content. You reply with JSON only."
)

_SECTION_TEMPLATE = """Document: {filename}
{summary_line}
Write a one-line description for each section below.

{sections}

Reply with a JSON object mapping each section id to its description:
{{"s0": "...", "s1": "..."}}"""

#: Characters of body text shown per section when asking for descriptions.
_SAMPLE_CHARS = 600
#: Sections per LLM call, to keep any single prompt bounded.
_BATCH = 25


def _section_key(draft: ChunkDraft) -> str:
    return " > ".join(draft.section_path)


def build_header(
    document_name: str, section_path: list[str], description: str
) -> str:
    parts = [document_name, *section_path]
    header = " > ".join(p for p in parts if p)
    return f"{header} - {description}" if description else header


async def _describe_sections(
    llm: LLM, filename: str, summary: str, samples: dict[str, str]
) -> dict[str, str]:
    """One call per batch of sections; returns {section key: description}."""
    keys = list(samples)
    described: dict[str, str] = {}

    for start in range(0, len(keys), _BATCH):
        batch = keys[start : start + _BATCH]
        ids = {f"s{i}": key for i, key in enumerate(batch)}
        rendered = "\n\n".join(
            f'[{sid}] {key or "(document root)"}\n{samples[key][:_SAMPLE_CHARS]}'
            for sid, key in ids.items()
        )
        prompt = _SECTION_TEMPLATE.format(
            filename=filename,
            summary_line=f"Document summary: {summary}" if summary else "",
            sections=rendered,
        )
        try:
            raw = await llm.complete(
                prompt, system=SYSTEM, json_mode=True, temperature=0.1
            )
            data = parse_json_object(raw)
        except Exception as exc:
            logger.warning("Section description batch failed for %s: %s", filename, exc)
            continue
        for sid, key in ids.items():
            value = data.get(sid)
            if isinstance(value, str) and value.strip():
                described[key] = value.strip()[:200]

    return described


async def contextualise(
    drafts: list[ChunkDraft],
    document_name: str,
    profile: DocumentProfile,
    llm: LLM | None,
    per_chunk: bool = False,
) -> list[ChunkDraft]:
    """Fill `contextual_header` on every draft. Mutates and returns `drafts`.

    With no LLM, headers still carry the document name and section path --
    the part that matters most, and the part that never costs a call.
    """
    if not drafts:
        return drafts

    descriptions: dict[str, str] = {}

    if llm is not None and per_chunk:
        for draft in drafts:
            try:
                raw = await llm.complete(
                    f"Document: {document_name}\n"
                    f"Section: {_section_key(draft) or '(document root)'}\n\n"
                    f"{draft.content[:2000]}\n\n"
                    'Reply with {"description": "one line, at most 15 words, '
                    'describing what this passage covers"}',
                    system=SYSTEM,
                    json_mode=True,
                    temperature=0.1,
                )
                value = parse_json_object(raw).get("description")
                if isinstance(value, str):
                    descriptions[str(draft.ordinal)] = value.strip()[:200]
            except Exception as exc:
                logger.warning("Chunk description failed (ordinal %d): %s",
                               draft.ordinal, exc)
        for draft in drafts:
            draft.contextual_header = build_header(
                document_name,
                draft.section_path,
                descriptions.get(str(draft.ordinal), ""),
            )
        return drafts

    if llm is not None:
        samples: dict[str, str] = {}
        for draft in drafts:
            key = _section_key(draft)
            if key not in samples:
                samples[key] = draft.content
        descriptions = await _describe_sections(
            llm, document_name, profile.summary, samples
        )

    for draft in drafts:
        draft.contextual_header = build_header(
            document_name, draft.section_path, descriptions.get(_section_key(draft), "")
        )
    return drafts


def header_preview(drafts: list[ChunkDraft], limit: int = 3) -> str:
    """Small helper for the seed script's output."""
    return json.dumps(
        [d.contextual_header for d in drafts[:limit]], indent=2, ensure_ascii=False
    )
