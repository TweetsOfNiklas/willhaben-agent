"""Own search agents: poll the search JSON, diff against stored hits, notify."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from sqlmodel import select

from app.config import get_settings
from app.db import Hit, SearchAgent, session, utcnow
from app.i18n import t
from app.services import scam, screening
from app.services.notify import notify
from app.willhaben.parse import AdSummary, describe_search, to_webapi_url
from app.willhaben.public import WillhabenError, get_public

log = logging.getLogger(__name__)

MAX_PUSH_PER_RUN = 5


@dataclass
class RunResult:
    agent_id: int
    rows_found: int = 0
    new: list[Hit] = field(default_factory=list)
    price_drops: list[Hit] = field(default_factory=list)
    bumped: int = 0
    baseline: bool = False
    error: str | None = None


def create_agent(source_url: str, name: str | None = None, interval_s: int | None = None) -> SearchAgent:
    s = get_settings()
    webapi = to_webapi_url(source_url, rows=s.search_rows)
    interval = max(interval_s or s.default_agent_interval_s, s.min_agent_interval_s)
    agent = SearchAgent(name=name or describe_search(source_url), source_url=source_url.strip(),
                        webapi_url=webapi, interval_s=interval)
    with session() as db:
        db.add(agent)
        db.commit()
        db.refresh(agent)
    return agent


def _apply(hit: Hit, ad: AdSummary) -> None:
    hit.heading = ad.heading
    hit.body = ad.body
    hit.price = ad.price
    hit.price_display = ad.price_display
    hit.old_price = ad.old_price
    hit.location = ad.location
    hit.postcode = ad.postcode
    hit.published_at = ad.published_at
    hit.end_date = ad.end_date
    hit.url = ad.url
    hit.image_url = ad.image_url
    hit.is_private = ad.is_private
    hit.last_seen_at = utcnow()


async def run_agent(agent_id: int) -> RunResult:
    res = RunResult(agent_id=agent_id)
    with session() as db:
        agent = db.get(SearchAgent, agent_id)
        if agent is None:
            res.error = t("Agent not found")
            return res
        webapi_url, name = agent.webapi_url, agent.name
    try:
        page = await get_public().search(webapi_url)
    except WillhabenError as e:
        with session() as db:
            agent = db.get(SearchAgent, agent_id)
            agent.last_run_at, agent.last_error = utcnow(), str(e)
            db.add(agent)
            db.commit()
        res.error = str(e)
        return res

    res.rows_found = page.rows_found
    with session() as db:
        agent = db.get(SearchAgent, agent_id)
        res.baseline = not agent.baseline_done
        existing = {h.ad_id: h for h in db.exec(select(Hit).where(Hit.agent_id == agent_id))}
        scam_ctx = _scam_context(existing.values(), page.ads)
        for ad in page.ads:
            if not ad.ad_id:
                continue
            hit = existing.get(ad.ad_id)
            if hit is None:
                hit = Hit(agent_id=agent_id, ad_id=ad.ad_id, heading=ad.heading, url=ad.url,
                          state="seen" if res.baseline else "new")
                _apply(hit, ad)
                hit.scam_flags = scam.flags(hit, scam_ctx) or None   # before screening, which sees them too
                db.add(hit)
                existing[ad.ad_id] = hit
                if not res.baseline:
                    res.new.append(hit)
                continue
            prev_price, prev_published = hit.price, hit.published_at
            _apply(hit, ad)
            if prev_price is not None and ad.price is not None and ad.price < prev_price:
                hit.price_dropped_at = utcnow()
                hit.old_price = prev_price
                hit.scam_flags = scam.flags(hit, scam_ctx) or None   # a new price can be a new red flag
                if hit.state != "ignored":
                    res.price_drops.append(hit)
            if ad.is_bumped and prev_published and ad.published_at and ad.published_at > prev_published:
                hit.bumped_count += 1
                res.bumped += 1
            db.add(hit)
        agent.last_run_at = agent.last_ok_at = utcnow()
        agent.last_error = None
        agent.last_rows_found = page.rows_found
        agent.baseline_done = True
        notify_new, notify_drops = agent.notify, agent.notify and agent.notify_price_drops
        db.add(agent)
        db.commit()
        for h in res.new + res.price_drops:
            db.refresh(h)
        db.refresh(agent)

    to_notify = (res.new if notify_new else []) + (res.price_drops if notify_drops else [])
    if agent.screen and to_notify:      # Claude: right item? good deal? (a new price is a new deal)
        await screening.screen(agent_id, to_notify)

    if notify_new and res.new:
        good = [h.id for h in res.new if screening.passes(agent, h)]
        pushed = set(good[:MAX_PUSH_PER_RUN])
        for h in res.new:
            if h.id in pushed or h.id not in good:   # rejected hits: HA event only, no push
                await notify("hit", _scam_prefix(h) + f"{name}: {h.price_display or ''}".strip(" :"), _hit_text(h),
                             url=h.url, image=h.image_url, push=h.id in pushed,
                             data={"agent": name, "ad_id": h.ad_id, "price": h.price, "location": h.location,
                                   **_screen_data(h), **_scam_data(h)})
        if len(good) > MAX_PUSH_PER_RUN:
            await notify("hit", name, t("+{n} more new hits", n=len(good) - MAX_PUSH_PER_RUN),
                         data={"agent": name, "count": len(good)})
    if notify_drops:
        for h in res.price_drops[:MAX_PUSH_PER_RUN]:
            await notify("price_drop", _scam_prefix(h) + t("{name}: price dropped", name=name),
                         f"{h.heading}: {h.old_price:g} → {h.price:g} €" + _scam_line(h) + _screen_line(h),
                         url=h.url, image=h.image_url,
                         push=screening.passes(agent, h),
                         data={"agent": name, "ad_id": h.ad_id, "price": h.price, "old_price": h.old_price,
                               **_screen_data(h), **_scam_data(h)})
    return res


def _screen_line(h: Hit) -> str:
    line = screening.summary(h)
    return f"\n{line}" if line else ""


def _hit_text(h: Hit) -> str:
    return f"{h.heading} ({h.location or '?'})" + _scam_line(h) + _screen_line(h)


def _screen_data(h: Hit) -> dict:
    """Screening result in the HA event, so automations can filter on it too."""
    if h.screen_relevant is None:
        return {}
    return {"screen_relevant": h.screen_relevant, "screen_deal": h.screen_deal, "screen_reason": h.screen_reason}


def _scam_context(hits, ads) -> dict:
    """Agent price median for the scam check: recent stored hits plus this page (newest price per ad)."""
    since = (utcnow() - timedelta(days=scam.CONTEXT_DAYS)).replace(tzinfo=None)   # SQLite gives naive UTC
    prices = {h.ad_id: h.price for h in hits if h.screen_relevant is not False
              and (h.first_seen_at is None or h.first_seen_at.replace(tzinfo=None) >= since)}
    prices.update({a.ad_id: a.price for a in ads if a.ad_id})
    return scam.median_context(list(prices.values()))


def _scam_prefix(h: Hit) -> str:
    return "⚠️ " if scam.warnings(h.scam_flags) else ""


def _scam_line(h: Hit) -> str:
    top = scam.headline(h.scam_flags)
    return f"\n⚠️ {top}" if top else ""


def _scam_data(h: Hit) -> dict:
    """Scam flag codes in the HA event (warn/high only), so automations can filter on them."""
    w = scam.warnings(h.scam_flags)
    return {"scam_flags": [f["code"] for f in w], "scam_level": scam.level(w)} if w else {}
