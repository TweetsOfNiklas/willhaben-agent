"""Small text helpers shared by the platform adapters (no I/O, no willhaben imports)."""
from __future__ import annotations

import html
import re
import unicodedata
from datetime import datetime, timezone


def clean_text(s: str | None) -> str | None:
    """HTML fragments as plain text: breaks and list items become lines, tags go, entities are unescaped."""
    if s is None:
        return None
    # a snippet cut off inside a tag ("… Nichtgebrauch. <br...", Kleinanzeigen's search list)
    s = re.sub(r"</?(?:br|p|div|span|li|ul|ol|b|i|u|em|strong|a|h[1-6])\b[^>]*$", "…", s, flags=re.I)
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


def one_line(s: str | None) -> str | None:
    """Tabs, line breaks and space runs as one space; None when nothing is left."""
    if s is None:
        return None
    s = re.sub(r"\s+", " ", str(s)).strip()
    return s or None


def fold(s: str | None) -> str:
    """Case-folded without accents: "Écho Show" -> "echo show"."""
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).casefold()


def mentions(text: str | None, words: str | None) -> bool:
    """Every word of `words` (a search term) is in `text`, whatever the case, accents, spaces and punctuation:
    "Pi5 8GB" mentions "pi 5", "Écho show-8" mentions "Echo Show 8". True without words."""
    hay = "".join(re.findall(r"[^\W_]+", fold(text)))
    return all(w in hay for w in re.findall(r"[^\W_]+", fold(words)))


def euro(amount: float) -> str:
    """German price text as the sites show it: 1.250 €, 12,50 €."""
    if amount == int(amount):
        return f"{amount:,.0f}".replace(",", ".") + " €"
    return f"{amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") + " €"


def from_epoch(v: float | int | str | None, *, ms: bool = False) -> datetime | None:
    """Unix time (seconds, or milliseconds with ms=True) -> aware UTC datetime; None for 0 or junk."""
    f = to_float(v)
    if not f:
        return None
    try:
        return datetime.fromtimestamp(f / 1000 if ms else f, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
