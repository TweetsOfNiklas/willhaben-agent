"""Staying on topic: code-side guard for automatic chat answers (seller and buyer).

Whatever Claude says in `auto_send`, an automatic answer is held back as a draft when it contains words of the agent's
own instructions or an AI talking about itself, or when it is far longer than a classified-ad chat needs - the typical
traces of a partner luring the agent into other topics. False positives only cost one tap on the approval push.
"""
from __future__ import annotations

import re

from app.i18n import t

# Words of the agent's instructions (schema fields, "prompt") and of an AI talking about itself
INTERNAL = (r"purchase_state|opening_offer|suggested_price|needs_info|auto_send|auto_reason|needs_user|"
            r"system-?prompt\w*|prompts?|sprachmodell\w*|language model|als (?:eine )?ki\b|"
            r"als (?:ein )?(?:ki-)?assistent\w*|as an ai\b")
_INTERNAL = re.compile(rf"\b(?:{INTERNAL})", re.I)
MAX_CHARS = 700                     # a chat answer on willhaben is a few sentences; longer ones wait for the user


def internal_word(text: str | None, pattern: re.Pattern[str] = _INTERNAL) -> str | None:
    """The first instruction or AI word in our own message, or None."""
    hit = pattern.search(text or "")
    return hit.group(0) if hit else None


def internal_problem(text: str | None, pattern: re.Pattern[str] = _INTERNAL) -> list[str]:
    word = internal_word(text, pattern)
    return [t("Internal instructions or AI talk in our own message ({word})", word=word)] if word else []


def too_long(text: str | None) -> list[str]:
    n = len(text or "")
    return [t("Answer is unusually long ({n} characters) – a chat answer needs a few sentences", n=n)] if n > MAX_CHARS else []


def blockers(text: str | None) -> list[str]:
    """Reasons to hold an automatic answer back: instruction or AI words, unusual length."""
    return internal_problem(text) + too_long(text)
