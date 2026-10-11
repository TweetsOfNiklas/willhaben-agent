"""Appointments agreed in willhaben chats -> Google Calendar, only after the user confirms.

Claude marks a fixed appointment in each reply draft (Draft.meetup). Once such an answer is sent, the
conversation remembers it (Conversation.meetup) and a push offers "Add to calendar". In the dashboard the
chat page shows a pre-filled form; for chats answered elsewhere Claude can look for the appointment.
Nothing is written to the calendar without a click.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from app.db import Conversation, Draft, session
from app.i18n import t
from app.services import gcal
from app.services import timeline

log = logging.getLogger(__name__)


def clean(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """Claude's meetup object -> {start, minutes, location}, or None if no (valid, future-ish) appointment."""
    if not raw or not (raw.get("start") or "").strip():
        return None
    try:
        start = gcal.parse_local(raw["start"])
    except gcal.CalendarError:
        return None
    if start < datetime.now(gcal.TZ) - timedelta(days=1):
        return None
    minutes = raw.get("minutes") or 30
    return {"start": start.strftime("%Y-%m-%dT%H:%M"), "minutes": min(max(int(minutes), 5), 480),
            "location": (raw.get("location") or "").strip()}


def summary_for(conv: Conversation) -> str:
    return f"willhaben: {conv.ad_title or t('Pickup')} – {conv.partner_name or '?'}"


def current(conv: Conversation, drafts: list[Draft]) -> dict[str, Any] | None:
    """What the chat page offers: the conversation's appointment, else the newest open draft's."""
    if conv.meetup:
        return conv.meetup
    return next((d.meetup for d in drafts if d.status in ("draft", "pending", "sending") and d.meetup), None)


def remember(conversation_id: str, meetup: dict[str, Any] | None) -> dict[str, Any] | None:
    """Store an appointment on the conversation; keeps the calendar link if the time is unchanged."""
    if not meetup:
        return None
    with session() as db:
        conv = db.get(Conversation, conversation_id)
        if conv is None:
            return None
        old = conv.meetup or {}
        if old.get("event_id") and old.get("start") == meetup["start"]:
            return old                                  # already in the calendar
        conv.meetup = {**meetup, "summary": summary_for(conv)}
        db.add(conv)
        db.commit()
        return conv.meetup


async def on_sent(draft: Draft) -> None:
    """After an answer went out: remember its appointment and offer it for the calendar (push/Telegram)."""
    m = remember(draft.conversation_id, draft.meetup)
    if not m or m.get("event_id") or not gcal.connected():
        return
    from app.config import get_settings
    from app.services import ha_actions
    from app.services.notify import notify

    with session() as db:
        conv = db.get(Conversation, draft.conversation_id)
    await notify("meetup", t("Add appointment to calendar?"),
                 f"{format_when(m)}{' · ' + m['location'] if m.get('location') else ''}\n{conv.ad_title or ''} · {conv.partner_name or ''}",
                 url=f"{get_settings().public_url.rstrip('/')}/chats/{conv.id}",
                 data={"conversation_id": conv.id, "ad_id": conv.ad_id, "draft_id": draft.id},
                 actions=ha_actions.meetup_actions(draft.id), tag=ha_actions.meetup_tag(draft.id))


def format_when(m: dict[str, Any]) -> str:
    start = gcal.parse_local(m["start"])
    end = start + timedelta(minutes=int(m.get("minutes") or 30))
    day = t(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][start.weekday()])
    return f"{day} {start:%d.%m. %H:%M}–{end:%H:%M}"


async def add(conversation_id: str, *, start: str, minutes: int, location: str = "", summary: str = "") -> dict[str, Any]:
    """Create the calendar entry and remember it on the conversation."""
    with session() as db:
        conv = db.get(Conversation, conversation_id)
    if conv is None:
        raise gcal.CalendarError(t("Conversation not found"))
    when = gcal.parse_local(start)
    minutes = min(max(int(minutes or 30), 5), 480)
    summary = summary.strip() or summary_for(conv)
    lines = [f"{conv.ad_title or ''} – {conv.partner_name or ''}".strip(" –")]
    if (conv.ad_context or {}).get("url"):
        lines.append(conv.ad_context["url"])
    lines.append("Chat: https://www.willhaben.at/iad/myprofile/chat")
    ev = await gcal.add_event(summary=summary, start=when, minutes=minutes, location=location.strip(),
                              description="\n".join(lines))
    m = {"start": when.strftime("%Y-%m-%dT%H:%M"), "minutes": minutes, "location": location.strip(),
         "summary": summary, "event_id": ev.get("id"), "event_link": ev.get("link")}
    with session() as db:
        conv = db.get(Conversation, conversation_id)
        conv.meetup = m
        db.add(conv)
        db.commit()
    log.info("calendar entry for %s at %s", conversation_id, m["start"])
    return m


async def add_from_draft(draft_id: int) -> dict[str, Any] | None:
    """Push button: add the appointment of a sent answer (once)."""
    with session() as db:
        d = db.get(Draft, draft_id)
        conv = db.get(Conversation, d.conversation_id) if d else None
    if d is None or conv is None or not d.meetup:
        return None
    if conv.meetup and conv.meetup.get("event_id") and conv.meetup.get("start") == d.meetup.get("start"):
        return None                                     # second tap
    return await add(conv.id, start=d.meetup["start"], minutes=d.meetup.get("minutes") or 30,
                     location=d.meetup.get("location") or "")


def forget(conversation_id: str) -> None:
    with session() as db:
        conv = db.get(Conversation, conversation_id)
        if conv:
            conv.meetup = None
            db.add(conv)
            db.commit()


@timeline.scoped(conversation_id="conversation_id")
async def extract(conversation_id: str) -> dict[str, Any] | None:
    """Ask Claude for the agreed appointment in a chat that was answered outside the app."""
    from app.llm.base import get_backend
    from app.llm.prompts import MEETUP_SCHEMA, MEETUP_SYSTEM, meetup_prompt
    from app.services.chats import messages_of

    with session() as db:
        conv = db.get(Conversation, conversation_id)
    if conv is None:
        return None
    msgs = [(m.direction, m.created_at, m.text) for m in messages_of(conversation_id)]
    if not msgs:
        return None
    res = await get_backend("chat").complete(system=MEETUP_SYSTEM, schema=MEETUP_SCHEMA, prompt=meetup_prompt(
        ad_title=conv.ad_title, partner=conv.partner_name, messages=msgs))
    return remember(conversation_id, clean(res.data))
