"""Replies to messages that are not questions about the documents.

"hi" is not a question about a runbook. Sent through retrieval it pulls the
nearest few chunks, the model rightly finds nothing in them that answers a
greeting, and the user is told "the documents do not cover that" -- correct,
and a baffling reply to hello.

So a small, deliberately narrow set of conversational messages is answered
here instead, with something useful: what this workspace actually contains.
Anything not on the list still goes to retrieval. Being narrow is the point --
a broad "is this small talk?" classifier would start swallowing real questions,
and a grounded assistant that skips its sources is worse than a clumsy one.

No LLM call is made, so these replies are instant and cost nothing.
"""

from __future__ import annotations

import re

from app.pipeline.answering import Answer

_GREETINGS = {
    "hi", "hii", "hiii", "hey", "heya", "hello", "helo", "hiya", "howdy", "yo",
    "good morning", "good afternoon", "good evening", "morning", "gm",
    "hi there", "hello there", "hey there",
}
_THANKS = {
    "thanks", "thank you", "thankyou", "thx", "ty", "tysm", "cheers", "ta",
    "thanks a lot", "thank you so much", "much appreciated", "appreciate it",
}
_ACKNOWLEDGEMENTS = {
    "ok", "okay", "k", "kk", "cool", "nice", "great", "got it", "alright",
    "sure", "fine", "perfect", "awesome", "understood",
}
_FAREWELLS = {"bye", "goodbye", "good bye", "see you", "see ya", "cya", "later"}
#: Questions about the assistant itself, which no document can answer.
_META = {
    "help", "what can i ask", "what can i ask you", "what should i ask",
    "how do i use this", "how does this work", "what is this",
    "what documents do you have", "which documents do you have",
    "what files do you have", "what do you know",
}

_PUNCTUATION = re.compile(r"[^\w\s]")
_SPACES = re.compile(r"\s+")


def _normalise(text: str) -> str:
    return _SPACES.sub(" ", _PUNCTUATION.sub(" ", text.lower())).strip()


def classify(text: str) -> str | None:
    """Return the kind of conversational message, or None for a real question.

    Exact matches only, after lowercasing and stripping punctuation. "hi, how do
    I escalate a P1?" is therefore a question, not a greeting.
    """
    key = _normalise(text)
    if key in _GREETINGS:
        return "greeting"
    if key in _THANKS:
        return "thanks"
    if key in _ACKNOWLEDGEMENTS:
        return "ack"
    if key in _FAREWELLS:
        return "farewell"
    if key in _META:
        return "meta"
    return None


def _join(items: list[str], limit: int) -> str:
    shown = items[:limit]
    if len(items) > limit:
        return ", ".join(shown) + f", and {len(items) - limit} more"
    if len(shown) > 1:
        return ", ".join(shown[:-1]) + f" and {shown[-1]}"
    return shown[0] if shown else ""


def reply(
    kind: str | None,
    ready_filenames: list[str],
    topics: list[str],
    processing: int = 0,
) -> Answer:
    """Build the reply for a conversational message.

    Grounded in the workspace's real contents -- the document names and the
    topic vocabulary profiling derived from them -- rather than a canned blurb,
    so it doubles as a hint about what is worth asking.
    """
    if kind == "thanks":
        return Answer(text="You're welcome. Ask me anything else about these documents.")
    if kind == "farewell":
        return Answer(text="Bye. Your conversation is saved until you clear it.")
    if kind == "ack":
        return Answer(text="Ask me a question about the documents whenever you're ready.")

    # A greeting, a question about the assistant, or (kind None) a real
    # question asked of a workspace with nothing ready to answer it from.
    hello = "Hi. " if kind == "greeting" else ""
    if not ready_filenames:
        if processing:
            return Answer(
                text=(
                    f"{hello}{processing} document(s) are still being processed. Once "
                    "they show ready on the left, ask me about them."
                )
            )
        return Answer(
            text=(
                f"{hello}This workspace has no documents yet. Upload one on the left, "
                "wait for it to show ready, then ask me about it."
            )
        )

    count = len(ready_filenames)
    noun = "document" if count == 1 else "documents"
    lines = [
        f"{hello}I answer questions using only the {count} {noun} in this workspace: "
        f"{_join(ready_filenames, 5)}."
    ]
    if topics:
        lines.append(f"They cover topics like {_join(topics, 6)}.")
    if processing:
        lines.append(f"{processing} more are still being processed.")
    lines.append(
        "Every answer cites its source, and if the documents don't cover "
        "something I'll say so rather than guess."
    )
    return Answer(text=" ".join(lines))
