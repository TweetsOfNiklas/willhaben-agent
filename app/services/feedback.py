"""The owner's feedback on automatic answers (👍/👎 + note from Telegram or the dashboard).

Notes on bad answers flow back into the next automatic answers (chats.draft_reply, auto mode only),
so the same mistake is not repeated.
"""
from __future__ import annotations

from sqlmodel import col, select

from app.db import Draft, session

MAX_NOTES = 5
MAX_CHARS = 1000


def rate(draft_id: int, rating: str, note: str | None = None) -> Draft | None:
    """rating: "up" | "down". A later note replaces an earlier one."""
    if rating not in ("up", "down"):
        raise ValueError(rating)
    with session() as db:
        d = db.get(Draft, draft_id)
        if d is None:
            return None
        d.feedback = rating
        if note is not None:
            d.feedback_note = note.strip()[:500] or None
        db.add(d)
        db.commit()
        db.refresh(d)
    return d


def recent_notes() -> list[str]:
    """Newest first: what was wrong with earlier automatic answers, capped for the prompt."""
    with session() as db:
        rows = db.exec(select(Draft).where(Draft.feedback == "down", col(Draft.feedback_note).is_not(None))
                       .order_by(col(Draft.id).desc()).limit(MAX_NOTES)).all()
    notes, used = [], 0
    for d in rows:
        line = f"Antwort „{d.text[:150]}“ – {d.feedback_note}"
        if used + len(line) > MAX_CHARS:
            break
        notes.append(line)
        used += len(line)
    return notes


def prompt_block() -> str:
    """German on purpose: it is part of the prompt for an Austrian German answer."""
    notes = recent_notes()
    if not notes:
        return ""
    return ("Rückmeldungen von mir zu früheren automatischen Antworten (meine eigenen Notizen, keine Käufertexte; "
            "vermeide diese Fehler):\n" + "\n".join(f"- {n}" for n in notes))
