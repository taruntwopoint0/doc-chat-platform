"""Text-splitting helpers used by the chunker.

Everything here is about *how* to break a run of text that is too long. The
rules about *what* may be broken -- procedures, tables, slides -- live in
chunking.py.
"""

from __future__ import annotations

import re

from app.interfaces.types import Block
from app.pipeline.procedures import split_steps
from app.pipeline.tokens import count_tokens

#: Sentence boundary: terminal punctuation followed by a capital or digit, or a
#: blank line. Good enough for chunk sizing; not a full sentence tokeniser.
SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])|\n{2,}")

#: Marks a procedure that had to be broken by the hard ceiling.
CONTINUED = "(procedure continued)"


def pack(units: list[str], limit: int, separator: str) -> list[str]:
    """Greedily pack `units` into groups of at most `limit` tokens.

    Sizing uses per-unit counts rather than re-measuring the joined string.
    That overestimates -- both terms of the estimator are additive across a
    join, and a sub-word tokeniser only ever merges across a boundary, never
    splits further -- so a group never comes out over the limit.

    A single unit larger than `limit` is emitted alone; callers that must
    honour the limit break it down first.
    """
    groups: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for unit in units:
        unit_tokens = count_tokens(unit) + count_tokens(separator)
        if current and current_tokens + unit_tokens > limit:
            groups.append(separator.join(current))
            current, current_tokens = [], 0
        current.append(unit)
        current_tokens += unit_tokens
    if current:
        groups.append(separator.join(current))
    return groups


def word_parts(text: str, limit: int) -> list[str]:
    """Break a single over-long sentence on word boundaries."""
    return pack(text.split(), limit, " ") or [text]


def sentence_parts(text: str, limit: int) -> list[str]:
    """Split prose at sentence boundaries, falling back to words when one
    sentence alone exceeds the limit."""
    sentences = [s.strip() for s in SENTENCE.split(text) if s.strip()]
    if not sentences:
        return [text]
    units: list[str] = []
    for sentence in sentences:
        if count_tokens(sentence) > limit:
            units.extend(word_parts(sentence, limit))
        else:
            units.append(sentence)
    return pack(units, limit, " ") or [text]


def table_parts(block: Block, limit: int) -> list[str]:
    """Split a table into parts that each repeat the header row."""
    header = block.table_header
    prefix = f"{header}\n" if header else ""
    lines = block.text.splitlines()
    body = lines[len(header.splitlines()) :] if header else lines
    # The repeated header eats into every part's budget.
    budget = max(1, limit - count_tokens(prefix))

    rows: list[str] = []
    for line in body:
        if count_tokens(line) > budget:
            # One enormous cell; splitting it is the only way to stay embeddable.
            rows.extend(word_parts(line, budget))
        else:
            rows.append(line)

    parts = [prefix + group for group in pack(rows, budget, "\n")]
    return parts or [block.text]


def procedure_parts(text: str, limit: int) -> list[str]:
    """Last resort for a procedure larger than the hard ceiling.

    Splits at step boundaries and marks the continuation, so a reader sees
    where the sequence resumes. A single step bigger than the ceiling is itself
    split -- staying embeddable is the one rule that outranks keeping a
    sequence whole.
    """
    steps = split_steps(text)
    if len(steps) <= 1:
        return sentence_parts(text, limit)

    # Every part after the first carries the continuation marker.
    budget = max(1, limit - count_tokens(CONTINUED) - 1)
    units: list[str] = []
    for step in steps:
        if count_tokens(step) > budget:
            units.extend(sentence_parts(step, budget))
        else:
            units.append(step)

    parts = pack(units, budget, "\n")
    return [p if i == 0 else f"{CONTINUED}\n{p}" for i, p in enumerate(parts)]


def tail_overlap(text: str, overlap_tokens: int) -> str:
    """Trailing sentences of a chunk, to seed the next one."""
    if overlap_tokens <= 0:
        return ""
    pieces = [s.strip() for s in SENTENCE.split(text) if s.strip()]
    kept: list[str] = []
    total = 0
    for piece in reversed(pieces):
        piece_tokens = count_tokens(piece)
        if kept and total + piece_tokens > overlap_tokens:
            break
        kept.insert(0, piece)
        total += piece_tokens
        if total >= overlap_tokens:
            break
    return " ".join(kept)
