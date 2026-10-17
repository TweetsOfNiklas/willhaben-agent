"""Editable system prompts: the owner's own version of what Claude is told for automatic answers (selling, buying) and
for the other side in the chat simulator; empty = the built-in default (app/llm/prompts.py), which a reset restores.

Kept in the settings `prompt_<kind>_system`. A saved version applies to real automatic answers at once (the
simulator's proposals let it be tried on the scenarios first). The leak check (app/services/topic.py) knows the
edited texts too.
"""
from __future__ import annotations

from collections.abc import Callable

from app.db import get_setting, set_setting


def _(text: str) -> str:
    """Marks a string for the German catalog; translated where it is shown (t())."""
    return text


def _auto() -> str:
    from app.llm import prompts
    return prompts.AUTO_REPLY_SYSTEM


def _purchase() -> str:
    from app.llm import prompts
    return prompts.PURCHASE_SYSTEM


def _partner() -> str:
    from app.llm import prompts
    return prompts.SIM_PARTNER_SYSTEM


# kind: (label, role it belongs to in the simulator, default)
KINDS: dict[str, tuple[str, str, Callable[[], str]]] = {
    "auto_reply": (_("Automatic answers when selling"), "seller", _auto),
    "purchase": (_("Automatic answers when buying"), "buyer", _purchase),
    "sim_partner": (_("The other side in the simulator"), "", _partner),
}
MAX_CHARS = 30000


def _key(kind: str) -> str:
    return f"prompt_{kind}_system"


def default(kind: str) -> str:
    return KINDS[kind][2]()


def custom(kind: str) -> bool:
    return bool((get_setting(_key(kind)) or "").strip())


def get(kind: str) -> str:
    """The prompt in use: the owner's version, else the default."""
    return (get_setting(_key(kind)) or "").strip() or default(kind)


def save(kind: str, text: str) -> None:
    """The owner's version; empty or the default itself means the default."""
    text = (text or "").replace("\r\n", "\n").strip()[:MAX_CHARS]
    set_setting(_key(kind), "" if text == default(kind).strip() else text)
    from app.services import topic
    topic.reset_cache()


def reset(kind: str) -> None:
    save(kind, "")


def custom_texts() -> list[str]:
    return [get(k) for k in KINDS if custom(k)]
