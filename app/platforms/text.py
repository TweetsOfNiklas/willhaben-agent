"""Small text helpers shared by the platform adapters (no I/O, no willhaben imports)."""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone


def clean_text(s: str | None) -> str | None:
    """HTML fragments as plain text: breaks and list items become lines, tags go, entities are unescaped."""
    if s is None:
        return None
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<li[^>]*>", "\n• ", s, flags=re.I)                      # dealers format with lists ...
    s = re.sub(r"</(p|div|ul|ol|h[1-6]|tr)>", "\n", s, flags=re.I)       # ... and paragraphs
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"[ \t]+\n", "\n", html.unescape(s))
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def parse_iso(v: str | None) -> datetime | None:
    """ISO strings like 2026-11-23T10:26:53+0100 -> aware UTC datetime (naive input is taken as UTC)."""
    if not v:
        return None
    try:
        dt = datetime.strptime(v, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_float(v: str | float | int | None) -> float | None:
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
