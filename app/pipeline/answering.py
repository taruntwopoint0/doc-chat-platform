"""Answer a question strictly from retrieved chunks.

Two rules define this stage, and both are about what the model must *not* do:
answers come only from the supplied passages, and when the passages do not
cover the question the model says so rather than filling the gap from its own
training. A grounded assistant that quietly guesses is worse than one that
refuses, because the user cannot tell the difference.

The refusal is enforced twice: the prompt asks for it, and `NO_ANSWER` is a
sentinel the code checks for, so a refusal is a structured outcome rather than
a sentence we hope the model produced.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from uuid import UUID

from app.interfaces.embedder import TASK_QUERY, Embedder
from app.interfaces.llm import LLM
from app.interfaces.types import SearchResult
from app.interfaces.vector_store import VectorStore

logger = logging.getLogger(__name__)

#: The model emits this exact token when the sources do not answer the question.
NO_ANSWER = "NO_ANSWER"

SYSTEM = f"""You answer questions using ONLY the numbered sources supplied with \
the question. You are not a general assistant and you must not use knowledge \
from your training.

Rules:
- Use only facts stated in the sources. Never invent a fact that is not there.
- Cite every claim with the source number in square brackets, like [2]. A \
sentence drawn from several sources cites each one, like [1][3].
- Answer whenever the sources address the subject of the question, even if \
they word it differently than the question does. A question about "P1 \
incidents" is answered by a section on incident escalation; a question about \
"holiday" is answered by a section on annual leave. Report what the sources \
actually say, in their words.
- If the sources cover the subject only partly, give the part they support, \
cite it, and say plainly what they do not cover. Prefer this over refusing.
- Reply with exactly {NO_ANSWER}, and nothing else, ONLY when the sources are \
about a genuinely different subject and contain nothing relevant. This is a \
last resort, not a way to avoid a hard question. Do not apologise or speculate.
- Quote identifiers, names, numbers and timings exactly as written.
- Be concise. Do not repeat the question back."""

_PROMPT = """Sources:

{sources}

Question: {question}

Answer using only the sources above, citing each claim like [1]. If they do not \
answer the question, reply with exactly {no_answer}."""

_CITATION = re.compile(r"\[(\d{1,2})\]")


@dataclass(slots=True)
class Citation:
    number: int
    document_id: UUID
    chunk_id: UUID
    filename: str
    section_path: list[str]
    snippet: str
    score: float


@dataclass(slots=True)
class Answer:
    text: str
    citations: list[Citation] = field(default_factory=list)
    #: True when the corpus did not support an answer.
    refused: bool = False
    #: Every passage considered, whether or not the model cited it.
    considered: int = 0


def build_sources(results: list[SearchResult]) -> str:
    blocks = []
    for index, result in enumerate(results, start=1):
        location = " > ".join(result.section_path) if result.section_path else ""
        heading = result.metadata.get("filename", "document")
        if location:
            heading = f"{heading} > {location}"
        blocks.append(f"[{index}] {heading}\n{result.content}")
    return "\n\n".join(blocks)


def _collect_citations(text: str, results: list[SearchResult]) -> list[Citation]:
    """Map the [n] markers the model actually used back to their chunks."""
    used: list[int] = []
    for match in _CITATION.finditer(text):
        number = int(match.group(1))
        if 1 <= number <= len(results) and number not in used:
            used.append(number)

    citations = []
    for number in used:
        result = results[number - 1]
        citations.append(
            Citation(
                number=number,
                document_id=result.document_id,
                chunk_id=result.chunk_id,
                filename=str(result.metadata.get("filename", "")),
                section_path=result.section_path,
                snippet=result.content[:400],
                score=result.score,
            )
        )
    return citations


#: Input that is conversation, not a question about the corpus. Matched exactly
#: after normalising, never as a prefix -- "hi, what is the escalation policy?"
#: is a real question and must go to retrieval like any other.
_CONVERSATIONAL = frozenset(
    {
        "hi", "hii", "hiii", "hey", "heyy", "hello", "helo", "hiya", "howdy",
        "yo", "sup", "good morning", "good afternoon", "good evening",
        "thanks", "thank you", "thanks a lot", "thankyou", "thx", "ty",
        "cheers", "ta", "nice", "cool", "great", "ok", "okay", "k", "got it",
        "bye", "goodbye", "see you", "see ya", "later",
        "help", "what is this", "what can i ask", "what can i ask you",
        "how do i use this", "how does this work",
    }
)

_PUNCTUATION = re.compile(r"[^\w\s]+")
_SPACES = re.compile(r"\s+")


def is_conversational(text: str) -> bool:
    """True for greetings and small talk, which retrieval cannot answer.

    Running these through the pipeline produces "the documents do not cover
    that", which is true but reads as though the product is broken.
    """
    normalised = _SPACES.sub(" ", _PUNCTUATION.sub("", text or "").lower()).strip()
    return normalised in _CONVERSATIONAL


def orientation(
    document_count: int, filenames: list[str], topics: list[str]
) -> Answer:
    """What to say instead: what is actually in here and what can be asked.

    Built from the workspace's own accumulated vocabulary, so it describes the
    real corpus rather than reciting a generic capability list.
    """
    if not document_count:
        return Answer(
            text=(
                "This workspace has no documents yet. Upload one on the left, "
                "wait for it to say ready, then ask me about it — I answer only "
                "from what you upload, and cite where each answer came from."
            )
        )

    shown = ", ".join(filenames[:5])
    if len(filenames) > 5:
        shown += f", and {len(filenames) - 5} more"

    lines = [
        f"I answer questions from the {document_count} document(s) in this "
        f"workspace: {shown}.",
    ]
    if topics:
        lines.append("They cover " + ", ".join(topics[:8]) + ".")
    lines.append(
        "Ask me something about them and I'll answer with citations, or tell "
        "you plainly if they don't cover it."
    )
    return Answer(text=" ".join(lines))


async def retrieve(
    store: VectorStore,
    embedder: Embedder,
    workspace_id: UUID,
    question: str,
    filters: dict | None = None,
    limit: int = 8,
) -> list[SearchResult]:
    """Embed the question and run hybrid search inside one workspace."""
    vector = (await embedder.embed_batch([question], TASK_QUERY))[0]
    return await store.hybrid_search(
        workspace_id=workspace_id,
        query=question,
        query_vector=vector,
        filters=filters or {},
        limit=limit,
    )


async def answer_question(
    llm: LLM,
    question: str,
    results: list[SearchResult],
) -> Answer:
    """Turn retrieved passages into a cited answer, or a refusal."""
    if not results:
        return Answer(
            text=(
                "I could not find anything about that in this workspace's "
                "documents."
            ),
            refused=True,
            considered=0,
        )

    prompt = _PROMPT.format(
        sources=build_sources(results),
        question=question.strip(),
        no_answer=NO_ANSWER,
    )
    raw = (await llm.complete(prompt, system=SYSTEM, temperature=0.0)).strip()

    # The sentinel may come back bare or wrapped in a sentence; either counts.
    if not raw or NO_ANSWER in raw.upper():
        return Answer(
            text=(
                "The documents in this workspace do not cover that. Try "
                "rephrasing, or upload a document that answers it."
            ),
            refused=True,
            considered=len(results),
        )

    return Answer(
        text=raw,
        citations=_collect_citations(raw, results),
        refused=False,
        considered=len(results),
    )
