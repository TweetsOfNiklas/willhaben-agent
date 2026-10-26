"""The weekly summary (Mondays 8:00, through the notification channels; off with the setting "weekly_digest" = "0"):
the last seven days in a few lines – new hits per agent, purchases, own ads sold, messages, requests the Geizhals cache
saved, Claude calls."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlmodel import col, select

from app.db import Hit, LLMCall, Message, MyAd, Purchase, SearchAgent, get_setting, session, utcnow
from app.i18n import t

log = logging.getLogger(__name__)
SETTING = "weekly_digest"


def on() -> bool:
    return get_setting(SETTING) != "0"


def lines(now: datetime | None = None) -> list[str]:
    """The summary of the seven days before `now`, one line per topic that has something to say."""
    now = now or utcnow()
    since = now - timedelta(days=7)
    out: list[str] = []
    with session() as db:
        per_agent = db.exec(select(SearchAgent.name, func.count()).join(Hit, Hit.agent_id == SearchAgent.id)
                            .where(Hit.first_seen_at >= since).group_by(SearchAgent.name)
                            .order_by(func.count().desc())).all()
        bought = db.exec(select(func.count()).select_from(Purchase).where(
            col(Purchase.status).in_(("paid", "done")), Purchase.updated_at >= since)).one()
        open_ = db.exec(select(func.count()).select_from(Purchase).where(
            col(Purchase.status).not_in(("paid", "done", "cancelled", "failed")))).one()
        sold = db.exec(select(func.count()).select_from(MyAd).where(MyAd.sale_status == "sold", MyAd.sale_at >= since)).one()
        incoming = db.exec(select(func.count()).select_from(Message).where(
            Message.direction == "in", col(Message.kind).is_(None), Message.created_at >= since)).one()
        calls = db.exec(select(func.count()).select_from(LLMCall).where(
            LLMCall.ts >= since, col(LLMCall.call_part).is_(None) | (LLMCall.call_part == 0))).one()
    total = sum(n for _, n in per_agent)
    if total:
        top = ", ".join(f"{name} {n}" for name, n in per_agent[:5])
        out.append(t("New hits: {n} ({agents})", n=total, agents=top))
    if bought or open_:
        out.append(t("Bought: {n} · open purchase orders: {m}", n=bought, m=open_))
    if sold:
        out.append(t("Own ads sold: {n}", n=sold))
    if incoming:
        out.append(t("Messages received: {n}", n=incoming))
    try:
        days = (json.loads(get_setting("geizhals_cache_stats") or "{}") or {}).get("days") or {}
    except ValueError:
        days = {}
    saved = sum(int(d.get("hits") or 0) for day, d in days.items() if day >= since.date().isoformat())
    if saved:
        out.append(t("Geizhals requests saved by the cache: {n}", n=saved))
    if calls:
        out.append(t("Claude calls: {n}", n=calls))
    return out


async def weekly_job() -> None:
    from app.services.notify import notify
    if not on():
        return
    body = lines()
    if not body:
        return
    await notify("digest", t("Your week"), "\n".join(body), url="/")

