"""Writing style (Settings → Selling → My rules → Writing style): how the owner writes, derived by Claude from their
own willhaben messages, plus the most characteristic of those messages as examples. Both go into every reply prompt
(chats.draft_reply → prompts.reply_prompt(style=…, examples=…)); the profile is a text the owner can edit.

Settings: `reply_style` (the profile, German lines), `reply_style_meta` (JSON: when it was derived, from how many
messages, the chosen examples).
"""
from __future__ import annotations

import json
from typing import Any

from app.db import get_setting

MAX_PROFILE = 2000        # characters of the profile text (a dozen short lines)


def profile() -> str:
    """The stored style profile ("" until derived or typed)."""
    return (get_setting("reply_style") or "").strip()


def meta() -> dict[str, Any]:
    """{at, count, examples} of the last derivation, {} without one."""
    try:
        data = json.loads(get_setting("reply_style_meta") or "")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def examples(limit: int | None = None) -> list[str]:
    """Style examples for the reply prompt: the profile's characteristic messages first, then the newest messages the
    owner typed themselves (chats._style_examples) to fill up to `limit` (prompts.MAX_STYLE_EXAMPLES), each once."""
    from app.llm.prompts import MAX_STYLE_EXAMPLES
    from app.services.chats import _style_examples

    limit = limit or MAX_STYLE_EXAMPLES
    out = [e.strip() for e in meta().get("examples") or [] if isinstance(e, str) and e.strip()][:limit]
    for e in _style_examples(limit):
        if len(out) >= limit:
            break
        if e not in out:
            out.append(e)
    return out


def panel() -> dict[str, Any]:
    """Template context of settings/_style.html (Jinja global `style_panel`)."""
    return {"profile": profile(), "meta": meta()}
