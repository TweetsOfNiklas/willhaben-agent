"""Optional Claude screening of search-agent hits: right product? good deal? Only hits that pass get a push.

Rejected hits stay in the dashboard and still fire the Home Assistant event (automations), just no push or
Telegram message. If Claude fails, hits are notified as before (fail open: no hit gets lost).
"""
from __future__ import annotations

import logging
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlmodel import col, select

from app.db import Hit, SearchAgent, session, utcnow
from app.i18n import LANGS, background_lang, t
from app.llm.base import LLMError, get_backend
from app.llm.prompts import SCREEN_SCHEMA, SCREEN_SYSTEM, screen_prompt
from app.services.ad_creator import price_stats

log = logging.getLogger(__name__)

MAX_PER_CALL = 10
MAX_IMAGE_BYTES = 3 * 1024 * 1024
DEAL_RANK = {"great": 3, "good": 2, "fair": 1, "unknown": 1, "overpriced": 0}
# agent threshold -> lowest deal that still notifies ("unknown" passes "fair" but not "good")
MIN_RANK = {"match": 0, "fair": 1, "good": 2}
_transport: httpx.AsyncBaseTransport | None = None   # tests inject a mock transport


def thresholds() -> dict[str, str]:
    return {"match": t("every matching item"), "fair": t("only fair or better prices"),
            "good": t("only good deals")}


def deal_label(deal: str | None) -> str:
    return {"great": t("top deal"), "good": t("good price"), "fair": t("fair price"),
            "overpriced": t("too expensive"), "unknown": t("price unclear")}.get(deal or "", "")


def passes(agent: SearchAgent, hit: Hit) -> bool:
    """Should this hit push? Unscreened hits (screening off, failed, not yet run) always do."""
    if not agent.screen or hit.screen_relevant is None:
        return True
    if not hit.screen_relevant:
        return False
    return DEAL_RANK.get(hit.screen_deal or "unknown", 1) >= MIN_RANK.get(agent.screen_min or "match", 0)


def summary(hit: Hit) -> str:
    """One line for the push text, e.g. "🤖 good price – 30 % under the usual used price"."""
    if hit.screen_relevant is None:
        return ""
    head = deal_label(hit.screen_deal) if hit.screen_relevant else t("wrong item")
    return f"🤖 {head}: {hit.screen_reason}" if hit.screen_reason else f"🤖 {head}"


def _stats(agent_id: int, exclude: set[int]) -> dict[str, Any] | None:
    since = utcnow() - timedelta(days=60)
    with session() as db:
        prices = db.exec(select(Hit.price).where(Hit.agent_id == agent_id, col(Hit.first_seen_at) >= since,
                                                 col(Hit.id).not_in(exclude or {0}), Hit.screen_relevant != False)  # noqa: E712
                         ).all()
    return price_stats([p for p in prices if p])


async def _download(url: str | None, folder: Path, name: str) -> Path | None:
    if not (url or "").startswith("https://"):
        return None
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True, transport=_transport) as c:
            r = await c.get(url)
        if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image/") \
                or len(r.content) > MAX_IMAGE_BYTES:
            return None
        ext = {"image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}.get(r.headers["content-type"].split(";")[0], ".jpg")
        p = folder / f"{name}{ext}"
        p.write_bytes(r.content)
        return p
    except httpx.HTTPError as e:
        log.info("screening image download failed: %s", e)
        return None


async def screen(agent_id: int, hits: list[Hit]) -> int:
    """Ask Claude about up to MAX_PER_CALL hits and store the verdicts on them. Returns how many were judged."""
    with session() as db:
        agent = db.get(SearchAgent, agent_id)
    if agent is None or not agent.screen or not hits:
        return 0
    hits = hits[:MAX_PER_CALL]
    stats = _stats(agent_id, {h.id for h in hits})
    lang = LANGS.get(background_lang(), "Deutsch")
    with tempfile.TemporaryDirectory(prefix="wh-screen-") as tmp:
        images: list[Path] = []
        items = []
        for nr, h in enumerate(hits, 1):
            img = await _download(h.image_url, Path(tmp), f"hit{nr}")
            if img:
                images.append(img)
            items.append({"nr": nr, "title": h.heading, "price": h.price_display or (f"{h.price:g} €" if h.price else None),
                          "location": h.location, "private": h.is_private, "description": h.body,
                          # the CLI wrapper copies photos in order as foto1, foto2, ...
                          "photo": f"foto{len(images)}{img.suffix}" if img else None})
        prompt = screen_prompt(agent_name=agent.name, brief=agent.screen_brief or "", hits=items, stats=stats,
                               language=lang)
        try:
            result = await get_backend("screen").complete(system=SCREEN_SYSTEM, prompt=prompt, schema=SCREEN_SCHEMA,
                                                          images=images)
        except LLMError as e:
            log.warning("screening failed for agent %s: %s", agent_id, e)
            return 0
    by_nr = {r.get("nr"): r for r in result.data.get("results") or [] if isinstance(r, dict)}
    judged = 0
    with session() as db:
        for nr, h in enumerate(hits, 1):
            r = by_nr.get(nr)
            if r is None:
                continue
            row = db.get(Hit, h.id)
            row.screen_relevant = bool(r.get("relevant"))
            row.screen_deal = r.get("deal") if r.get("deal") in DEAL_RANK else "unknown"
            row.screen_reason = (r.get("reason") or "").strip()[:200] or None
            row.screen_at = utcnow()
            db.add(row)
            judged += 1
        db.commit()
    for h in hits:          # callers keep using their objects
        with session() as db:
            fresh = db.get(Hit, h.id)
        h.screen_relevant, h.screen_deal, h.screen_reason = fresh.screen_relevant, fresh.screen_deal, fresh.screen_reason
    return judged
