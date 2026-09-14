"""Chunking boundary rules.

These are the rules that make retrieval trustworthy: a procedure cut in half
answers a question wrongly rather than not at all, and a table row without its
header is unreadable on its own.
"""

from __future__ import annotations

import pytest

from app.interfaces.types import Block, BlockKind
from app.pipeline.chunking import ChunkingConfig, chunk_blocks
from app.pipeline.procedures import group_numbered_procedures, step_number
from app.pipeline.tokens import count_tokens

# Pure logic: no database involved.
pytestmark = pytest.mark.no_db

TIGHT = ChunkingConfig(target_tokens=60, overlap_ratio=0.15, max_tokens=400)


def text_block(text: str, **kwargs) -> Block:
    return Block(kind=BlockKind.TEXT, text=text, **kwargs)


def filler(words: int, word: str = "policy") -> str:
    return " ".join([word] * words) + "."


# --- numbered procedures -------------------------------------------------


PROCEDURE = [
    "1. Open the incident record.",
    "2. Set the priority to P2.",
    "3. Page the on-call engineer.",
    "4. Start a bridge call.",
    "5. Record the timeline in the ticket.",
]


def test_step_number_recognises_common_markers():
    assert step_number("1. Do the thing") == 1
    assert step_number("Step 4 - verify") == 4
    assert step_number("(12) final check") == 12
    assert step_number("2) restart the service") == 2
    assert step_number("Nothing numbered here") is None


def test_consecutive_numbered_items_merge_into_one_atomic_block():
    blocks = [Block(kind=BlockKind.LIST, text=t) for t in PROCEDURE]
    grouped = group_numbered_procedures(blocks)
    assert len(grouped) == 1
    assert grouped[0].atomic is True
    assert grouped[0].text.splitlines() == PROCEDURE


def test_repeated_number_one_is_not_a_procedure():
    """Bullets that all start '1.' are separate one-step lists, not a sequence."""
    blocks = [Block(kind=BlockKind.LIST, text="1. Standalone note") for _ in range(4)]
    grouped = group_numbered_procedures(blocks)
    assert len(grouped) == 4
    assert not any(b.atomic for b in grouped)


def test_procedure_is_never_split_across_chunks():
    blocks = [
        text_block(filler(120)),
        *[Block(kind=BlockKind.LIST, text=t) for t in PROCEDURE],
        text_block(filler(120)),
    ]
    chunks = chunk_blocks(blocks, TIGHT)

    holders = [c for c in chunks if "1. Open the incident record." in c.content]
    assert len(holders) == 1, "the procedure's first step appears in exactly one chunk"
    whole = holders[0].content
    for step in PROCEDURE:
        assert step in whole, f"{step!r} was separated from the rest of the procedure"


def test_procedure_stays_whole_even_when_it_exceeds_the_target():
    # Six short steps: over the 60-token target, comfortably under the ceiling.
    long_steps = [f"{i}. {filler(12)}" for i in range(1, 7)]
    blocks = [Block(kind=BlockKind.LIST, text=t) for t in long_steps]
    chunks = chunk_blocks(blocks, TIGHT)
    assert len(chunks) == 1
    assert chunks[0].token_count > TIGHT.target_tokens


def test_oversized_procedure_splits_only_at_step_boundaries():
    """The embedder's hard limit is the one thing that may break a procedure."""
    # Each step fits the ceiling on its own; the whole sequence does not.
    steps = [f"{i}. {filler(60)}" for i in range(1, 9)]
    blocks = [Block(kind=BlockKind.LIST, text=t) for t in steps]
    chunks = chunk_blocks(blocks, TIGHT)

    assert len(chunks) > 1
    assert all(c.token_count <= TIGHT.max_tokens for c in chunks)
    for chunk in chunks:
        body = chunk.content.replace("(procedure continued)\n", "")
        assert step_number(body) is not None, "a part began mid-step"
    for chunk in chunks[1:]:
        assert chunk.content.startswith("(procedure continued)")


# --- tables --------------------------------------------------------------


def build_table(rows: int, cell_words: int = 1) -> Block:
    header = "| Code | Description |"
    separator = "| --- | --- |"
    body = [
        f"| C{i:03d} | {filler(cell_words, 'detail')} |" for i in range(1, rows + 1)
    ]
    return Block(
        kind=BlockKind.TABLE,
        text="\n".join([header, separator, *body]),
        table_header=f"{header}\n{separator}",
    )


def test_small_table_stays_in_one_chunk_with_its_header():
    chunks = chunk_blocks([build_table(3)], TIGHT)
    assert len(chunks) == 1
    assert "| Code | Description |" in chunks[0].content


def test_split_table_repeats_the_header_on_every_part():
    table = build_table(rows=40, cell_words=6)
    chunks = chunk_blocks([table], TIGHT)

    assert len(chunks) > 1, "the table should have been split"
    for chunk in chunks:
        lines = chunk.content.splitlines()
        assert lines[0] == "| Code | Description |", "part is missing its header row"
        assert lines[1] == "| --- | --- |", "part is missing the header separator"
        assert len(lines) > 2, "a part contained only a header"


def test_every_table_row_survives_the_split_exactly_once():
    table = build_table(rows=40, cell_words=6)
    chunks = chunk_blocks([table], TIGHT)
    joined = "\n".join(c.content for c in chunks)
    for i in range(1, 41):
        assert joined.count(f"| C{i:03d} |") == 1


def test_table_is_not_merged_with_following_prose_when_split():
    blocks = [build_table(rows=40, cell_words=6), text_block(filler(30), )]
    chunks = chunk_blocks(blocks, TIGHT)
    table_chunks = [c for c in chunks if "| C001 |" in c.content]
    assert "policy" not in table_chunks[0].content


# --- slides --------------------------------------------------------------


def slide_blocks() -> list[Block]:
    blocks: list[Block] = []
    for index in range(1, 4):
        blocks.append(
            Block(
                kind=BlockKind.HEADING,
                text=f"Slide title {index}",
                section_path=[f"Slide title {index}"],
                level=1,
                page=index,
                slide_index=index,
            )
        )
        blocks.append(
            Block(
                kind=BlockKind.TEXT,
                text=filler(15, f"body{index}"),
                section_path=[f"Slide title {index}"],
                page=index,
                slide_index=index,
            )
        )
        blocks.append(
            Block(
                kind=BlockKind.SLIDE_NOTES,
                text=f"Speaker notes: note {index}",
                section_path=[f"Slide title {index}"],
                page=index,
                slide_index=index,
            )
        )
    return blocks


def test_one_chunk_per_slide_including_speaker_notes():
    chunks = chunk_blocks(slide_blocks(), TIGHT)
    assert len(chunks) == 3
    for index, chunk in enumerate(chunks, start=1):
        assert chunk.slide_index == index
        assert f"Slide title {index}" in chunk.content
        assert f"Speaker notes: note {index}" in chunk.content


def test_slides_never_merge_even_when_tiny():
    blocks = [
        Block(
            kind=BlockKind.TEXT,
            text="Short.",
            page=i,
            slide_index=i,
            section_path=[f"S{i}"],
        )
        for i in range(1, 6)
    ]
    chunks = chunk_blocks(blocks, TIGHT)
    assert len(chunks) == 5


def test_slide_content_does_not_bleed_between_chunks():
    chunks = chunk_blocks(slide_blocks(), TIGHT)
    for index, chunk in enumerate(chunks, start=1):
        others = {1, 2, 3} - {index}
        for other in others:
            assert f"body{other}" not in chunk.content


# --- packing, overlap and ordering ---------------------------------------


def test_prose_is_packed_towards_the_target():
    blocks = [text_block(filler(20, f"w{i}")) for i in range(12)]
    chunks = chunk_blocks(blocks, TIGHT)
    assert len(chunks) > 1
    for chunk in chunks[:-1]:
        assert chunk.token_count <= TIGHT.max_tokens


def test_consecutive_chunks_share_overlapping_text():
    sentences = [f"Sentence number {i} about the service desk." for i in range(40)]
    blocks = [text_block(" ".join(sentences))]
    chunks = chunk_blocks(blocks, TIGHT)
    assert len(chunks) > 1
    first_words = set(chunks[0].content.split())
    assert first_words & set(chunks[1].content.split())


def test_ordinals_are_dense_and_sequential():
    blocks = [text_block(filler(25, f"w{i}")) for i in range(15)]
    chunks = chunk_blocks(blocks, TIGHT)
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))


def test_section_path_is_carried_onto_chunks():
    blocks = [
        Block(
            kind=BlockKind.HEADING,
            text="Escalation",
            section_path=["Incidents", "Escalation"],
            level=2,
        ),
        text_block(filler(20), section_path=["Incidents", "Escalation"]),
    ]
    chunks = chunk_blocks(blocks, TIGHT)
    assert chunks[0].section_path == ["Incidents", "Escalation"]


def test_empty_input_produces_no_chunks():
    assert chunk_blocks([], TIGHT) == []
    assert chunk_blocks([text_block("   ")], TIGHT) == []


@pytest.mark.parametrize("target", [100, 300, 500])
def test_no_chunk_exceeds_the_hard_ceiling(target):
    config = ChunkingConfig(target_tokens=target, max_tokens=target * 2)
    blocks = [text_block(filler(400, f"w{i}")) for i in range(5)]
    chunks = chunk_blocks(blocks, config)
    assert chunks
    for chunk in chunks:
        assert count_tokens(chunk.content) <= config.max_tokens
