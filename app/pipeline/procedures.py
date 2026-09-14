"""Detect numbered procedures so the chunker can keep them whole.

A procedure is a run of consecutive list-ish blocks whose leading markers form
an ascending numeric sequence -- "1. ... 2. ... 3. ..." or "Step 1 ... Step 2".
Ascent matters: a document where every bullet starts "1." is a set of separate
one-step lists, not one procedure, and merging those would be wrong.
"""

from __future__ import annotations

import re

from app.interfaces.types import Block, BlockKind

_MARKERS = (
    re.compile(r"^\s*(?P<n>\d{1,3})\s*[.)\]]\s+\S"),
    re.compile(r"^\s*step\s+(?P<n>\d{1,3})\b", re.IGNORECASE),
    re.compile(r"^\s*\(\s*(?P<n>\d{1,3})\s*\)\s+\S"),
)

#: Kinds that can take part in a procedure. A table or an image between two
#: steps is content belonging to the step above it, so it does not break the run.
_CONTINUABLE = {BlockKind.LIST, BlockKind.TEXT, BlockKind.CODE, BlockKind.TABLE}


def step_number(text: str) -> int | None:
    """Return the step number a line opens with, or None."""
    first_line = text.lstrip().splitlines()[0] if text.strip() else ""
    for pattern in _MARKERS:
        match = pattern.match(first_line)
        if match:
            return int(match.group("n"))
    return None


def split_steps(text: str) -> list[str]:
    """Split already-merged procedure text back into its individual steps.

    Used only as a last resort, when one procedure exceeds the hard token
    ceiling and has to be broken at a step boundary.
    """
    steps: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if step_number(line) is not None and current:
            steps.append("\n".join(current).rstrip())
            current = [line]
        else:
            current.append(line)
    if current:
        steps.append("\n".join(current).rstrip())
    return [s for s in steps if s.strip()]


def _is_ascending_run(numbers: list[int]) -> bool:
    """A genuine procedure starts at 0 or 1 and climbs."""
    if len(numbers) < 2 or numbers[0] > 1:
        return False
    return all(b > a for a, b in zip(numbers, numbers[1:], strict=False))


def group_numbered_procedures(blocks: list[Block]) -> list[Block]:
    """Merge each detected procedure into a single atomic Block.

    Returns a new list; the input is not modified.
    """
    grouped: list[Block] = []
    index = 0

    while index < len(blocks):
        block = blocks[index]
        if block.kind not in _CONTINUABLE or step_number(block.text) is None:
            grouped.append(block)
            index += 1
            continue

        run = [block]
        numbers = [step_number(block.text)]
        cursor = index + 1
        while cursor < len(blocks):
            candidate = blocks[cursor]
            if candidate.kind not in _CONTINUABLE:
                break
            number = step_number(candidate.text)
            if number is None:
                # An unnumbered paragraph continues the current step only if a
                # further numbered step follows it.
                lookahead = cursor + 1
                if (
                    lookahead < len(blocks)
                    and blocks[lookahead].kind in _CONTINUABLE
                    and (step_number(blocks[lookahead].text) or 0) > (numbers[-1] or 0)
                ):
                    run.append(candidate)
                    cursor += 1
                    continue
                break
            if number <= (numbers[-1] or 0):
                break
            run.append(candidate)
            numbers.append(number)
            cursor += 1

        actual = [n for n in numbers if n is not None]
        if _is_ascending_run(actual):
            grouped.append(
                Block(
                    kind=BlockKind.LIST,
                    text="\n".join(b.text for b in run),
                    section_path=list(block.section_path),
                    page=block.page,
                    slide_index=block.slide_index,
                    atomic=True,
                )
            )
            index = cursor
        else:
            grouped.append(block)
            index += 1

    return grouped
