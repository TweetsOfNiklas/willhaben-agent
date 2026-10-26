"""UI translations. Source strings are English; German lives in app/i18n/de/*.json (English -> German).

Use `t("Saved {n} drafts", n=3)` in Python and `{{ _("Saved {n} drafts", n=3) }}` in templates.
The language comes from the current request (set by middleware) or, for background work such as
Home Assistant pushes, from the `ui_lang` setting; "auto" follows the browser's Accept-Language.
"""
from __future__ import annotations

import json
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path

from app.i18n.plural import SINGULAR

LANGS = {"en": "English", "de": "Deutsch"}
DEFAULT = "en"
_current: ContextVar[str | None] = ContextVar("ui_lang", default=None)


@lru_cache
def catalog(lang: str) -> dict[str, str]:
    """All fragments of a language merged (one JSON file per area keeps parallel edits apart)."""
    merged: dict[str, str] = {}
    for f in sorted((Path(__file__).parent / lang).glob("*.json")):
        merged.update(json.loads(f.read_text(encoding="utf-8")))
    return merged


def preference() -> str:
    from app.db import get_setting
    return get_setting("ui_lang") or "auto"


def background_lang() -> str:
    """Language for texts produced outside a request (pushes, scheduled jobs, event log)."""
    from app.db import get_setting
    pref = preference()
    if pref in LANGS:
        return pref
    seen = get_setting("ui_lang_seen")
    return seen if seen in LANGS else DEFAULT


def from_accept_language(header: str | None) -> str:
    for part in (header or "").split(","):
        code = part.split(";")[0].strip().lower()[:2]
        if code in LANGS:
            return code
    return DEFAULT


def lang() -> str:
    return _current.get() or background_lang()


def set_request_lang(code: str) -> None:
    _current.set(code if code in LANGS else DEFAULT)


def t(text: str, /, **kwargs) -> str:
    code = lang()
    if kwargs.get("n") == 1 and text in SINGULAR:      # "1 comparable ad", not "1 comparable ads"
        text = SINGULAR[text]
    out = catalog(code).get(text, text) if code != "en" else text
    return out.format(**kwargs) if kwargs else out
