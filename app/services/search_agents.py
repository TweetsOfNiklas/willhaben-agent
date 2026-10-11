"""Own search agents: poll the search JSON, diff against stored hits, notify.

An agent is either one willhaben search URL or, created from a product search, several keyword searches
(`queries`) run one after another per check, with the search plan's cheap code filters (`plan`) applied to new ads.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlmodel import col, select

from app.config import get_settings
from app.db import Hit, ProductSearch, SearchAgent, get_setting, session, set_setting, utcnow
from app.i18n import t
from app.services import product_search, scam, screening
from app.services.notify import notify
from app.willhaben.parse import AdSummary, describe_search, keyword_search_url, to_webapi_url, web_url
from app.willhaben.public import Blocked, WillhabenError, get_public

log = logging.getLogger(__name__)

MAX_PUSH_PER_RUN = 5
NAME_CHARS = 60
SETTING_REFILED = "hits_filtered_refiled"    # refile_screened() ran


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


def agent_queries(plan: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The agent's searches for a product-search plan: newest first (agents diff on newness), no price_to in the
    URL (drop_reason applies it, like the product search does)."""
    rows = get_settings().search_rows
    out = []
    for q in (plan or {}).get("queries") or []:
        if not isinstance(q, dict) or not q.get("keyword"):
            continue
        path = q.get("category_path") or product_search.DEFAULT_PATH
        webapi = keyword_search_url(q["keyword"], rows, q.get("price_from"), None, sort=1, path=path)
        out.append({"keyword": q["keyword"], "webapi_url": webapi, "source_url": web_url(webapi),
                    "price_from": q.get("price_from"), "price_to": q.get("price_to"),
                    "category_path": q.get("category_path"), "why": q.get("why") or ""})
    return out


def default_interval_s(n_queries: int) -> int:
    """An agent sends one request per search and check, so it checks n times less often by default."""
    s = get_settings()
    return max(s.default_agent_interval_s * max(n_queries, 1), s.min_agent_interval_s)


def agent_name(row: ProductSearch) -> str:
    text = ((row.plan or {}).get("summary") or row.description or "").strip() or t("search")
    text = " ".join(text.split())
    if len(text) <= NAME_CHARS:
        return text
    cut = text[:NAME_CHARS].rsplit(" ", 1)[0].rstrip(" ,.;:–-")
    return (cut or text[:NAME_CHARS]) + "…"


def screen_brief(row: ProductSearch) -> str:
    """Screening brief from the search (goes into Claude's prompt, so German is fine; editable later)."""
    plan = row.plan or {}
    lines = [(row.description or "").strip()]
    if plan.get("max_price"):
        lines.append(f"Budget: bis {plan['max_price']:g} €")
    for label, key in (("Muss", "must_have"), ("Wünsche", "nice_to_have"), ("Ausschluss", "exclude")):
        if plan.get(key):
            lines.append(f"{label}: {', '.join(plan[key])}")
    lines.append(product_search.preferences_text(plan, row.answers))   # the person's answers to the questions
    return "\n".join(x for x in lines if x)


def agent_plan(plan: dict[str, Any] | None) -> dict[str, Any]:
    """What product_search.drop_reason needs at check time."""
    plan = plan or {}
    return {"summary": plan.get("summary") or "", "max_price": plan.get("max_price"),
            "must_have": list(plan.get("must_have") or []), "nice_to_have": list(plan.get("nice_to_have") or []),
            "exclude": list(plan.get("exclude") or [])}


def create_agent_from_search(search_id: int, name: str | None = None, interval_s: int | None = None,
                             screen: bool = True) -> SearchAgent:
    """A search agent that keeps running a finished product search's keyword searches. Raises KeyError for an
    unknown search, ValueError when it has no planned searches. Does not run the baseline (the caller schedules)."""
    with session() as db:
        row = db.get(ProductSearch, search_id)
    if row is None:
        raise KeyError(search_id)
    queries = agent_queries(row.plan)
    if not queries:
        raise ValueError(t("This search has no planned willhaben searches yet."))
    s = get_settings()
    interval = max(interval_s or default_interval_s(len(queries)), s.min_agent_interval_s)
    first = queries[0]
    agent = SearchAgent(name=(name or "").strip() or agent_name(row), source_url=first["source_url"],
                        webapi_url=first["webapi_url"], interval_s=interval, queries=queries,
                        plan=agent_plan(row.plan), search_id=row.id)
    if screen:
        agent.screen, agent.screen_brief, agent.screen_min = True, screen_brief(row) or None, "match"
    with session() as db:
        db.add(agent)
        db.commit()
        db.refresh(agent)
    return agent


async def _search_all(queries: list[dict[str, Any]]) -> tuple[list[tuple[AdSummary, dict[str, Any]]], int,
                                                             list[str], Exception | None, int]:
    """Run an agent's searches one after another (uncached). Returns (ads with their query, de-duplicated by ad id,
    first occurrence wins; summed rows_found; error lines; first exception; number of searches that succeeded).
    A block stops the remaining searches of this run."""
    client = get_public()
    ads: list[tuple[AdSummary, dict[str, Any]]] = []
    seen: set[str] = set()
    rows, errors, first_exc, ok = 0, [], None, 0
    n = len(queries)
    for i, q in enumerate(queries, 1):
        try:
            page = await client.search(q["webapi_url"])
        except WillhabenError as e:
            log.warning("agent search %s/%s (%s) failed: %s", i, n, q.get("keyword"), e)
            errors.append(t("search {i}/{n} failed: {error}", i=i, n=n, error=e))
            first_exc = first_exc or e
            if isinstance(e, Blocked):
                if i < n:
                    errors.append(t("remaining searches skipped: {k}", k=n - i))
                break
            continue
        ok += 1
        rows += page.rows_found or 0
        for ad in page.ads:
            if not ad.ad_id or ad.ad_id in seen:
                continue
            seen.add(ad.ad_id)
            ads.append((ad, q))
    return ads, rows, errors, first_exc, ok


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
    """One check of a search agent, as one timeline run (its searches, screening and pushes are the steps)."""
    from app.services import timeline

    with session() as db:
        agent = db.get(SearchAgent, agent_id)
        title = agent.name if agent else t("Agent {id}", id=agent_id)
    async with timeline.run("agent_run", title, name=f"agent-{agent_id}", agent_id=agent_id) as tl:
        res = await _run_agent(agent_id)
        tl.finish(not res.error, res.error, payload={k: v for k, v in {
            "rows_found": res.rows_found, "new": len(res.new), "price_drops": len(res.price_drops), "bumped": res.bumped,
            "baseline": res.baseline or None}.items() if v is not None})
    return res


async def _run_agent(agent_id: int) -> RunResult:
    res = RunResult(agent_id=agent_id)
    with session() as db:
        agent = db.get(SearchAgent, agent_id)
        if agent is None:
            res.error = t("Agent not found")
            return res
        webapi_url, name = agent.webapi_url, agent.name
        queries, plan = agent.queries, agent.plan
    partial_error = None
    if queries:      # created from a product search: several searches per check
        found, rows_found, errors, first_exc, ok = await _search_all(queries)
        if not ok:
            e = first_exc       # every search failed: the same error path as a single-URL agent
            with session() as db:
                agent = db.get(SearchAgent, agent_id)
                agent.last_run_at, agent.last_error = utcnow(), str(e)
                db.add(agent)
                db.commit()
            res.error = str(e)
            return res
        partial_error = "; ".join(errors) or None
    else:
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
        found, rows_found = [(ad, None) for ad in page.ads], page.rows_found

    res.rows_found = rows_found
    with session() as db:
        agent = db.get(SearchAgent, agent_id)
        res.baseline = not agent.baseline_done
        existing = {h.ad_id: h for h in db.exec(select(Hit).where(Hit.agent_id == agent_id))}
        scam_ctx = _scam_context(existing.values(), [ad for ad, _ in found])
        for ad, query in found:
            if not ad.ad_id:
                continue
            hit = existing.get(ad.ad_id)
            if hit is None:
                if plan and product_search.drop_reason(ad, plan, query):
                    continue        # "Suche" ad, far over budget or an exclusion word: never stored or notified
                hit = Hit(agent_id=agent_id, ad_id=ad.ad_id, heading=ad.heading, url=ad.url,
                          state="seen" if res.baseline else "new")
                _apply(hit, ad)
                hit.scam_flags = scam.flags(hit, scam_ctx) or None   # before screening, which sees them too
                _mark_scam_issue(hit)
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
                _mark_scam_issue(hit)
                if hit.state != "ignored":
                    res.price_drops.append(hit)
            if ad.is_bumped and prev_published and ad.published_at and ad.published_at > prev_published:
                hit.bumped_count += 1
                res.bumped += 1
            db.add(hit)
        agent.last_run_at = agent.last_ok_at = utcnow()
        agent.last_error = partial_error
        agent.last_rows_found = rows_found
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
        _file_screened(agent, res.new, res.price_drops)

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


def _file_screened(agent: SearchAgent, new: list[Hit], drops: list[Hit]) -> None:
    """A new hit the screening holds back gets no push, so it goes to "filtered" instead of "new"; a filtered hit
    that passes after a price drop is pushed and becomes new again."""
    moves = [(h, "new", "filtered") for h in new if not screening.passes(agent, h)]
    moves += [(h, "filtered", "new") for h in drops if screening.passes(agent, h)]
    if not moves:
        return
    with session() as db:
        for h, old, state in moves:
            row = db.get(Hit, h.id)
            if row is not None and row.state == old:      # unless it was saved, seen or ignored meanwhile
                row.state = h.state = state
                db.add(row)
        db.commit()


def refile_screened() -> None:
    """Once, at startup: new hits the screening held back before the "filtered" state existed move there."""
    if get_setting(SETTING_REFILED):
        return
    with session() as db:
        agents = {a.id: a for a in db.exec(select(SearchAgent).where(SearchAgent.screen == True))}  # noqa: E712
        for h in db.exec(select(Hit).where(Hit.state == "new", col(Hit.screen_relevant).is_not(None))):
            if h.agent_id in agents and not screening.passes(agents[h.agent_id], h):
                h.state = "filtered"
                db.add(h)
        db.commit()
    set_setting(SETTING_REFILED, "1")


def _screen_line(h: Hit) -> str:
    line = screening.summary(h)
    return f"\n{line}" if line else ""


def _hit_text(h: Hit) -> str:
    return f"{h.heading} ({h.location or '?'})" + _scam_line(h) + _screen_line(h)


def _screen_data(h: Hit) -> dict:
    """Screening result in the HA event, so automations can filter on it too."""
    if h.screen_relevant is None:
        return {}
    return {"screen_relevant": h.screen_relevant, "screen_deal": h.screen_deal, "screen_reason": h.screen_reason,
            "screen_issues": h.screen_issues or []}


def _scam_context(hits, ads) -> dict:
    """Agent price median for the scam check: recent stored hits plus this page (newest price per ad)."""
    since = (utcnow() - timedelta(days=scam.CONTEXT_DAYS)).replace(tzinfo=None)   # SQLite gives naive UTC
    prices = {h.ad_id: h.price for h in hits if h.screen_relevant is not False
              and (h.first_seen_at is None or h.first_seen_at.replace(tzinfo=None) >= since)}
    rejected = {h.ad_id for h in hits if h.screen_relevant is False}
    prices.update({a.ad_id: a.price for a in ads if a.ad_id and a.ad_id not in rejected})
    return scam.median_context(list(prices.values()))


def _mark_scam_issue(h: Hit) -> None:
    """A severe rule-based flag counts as screening reason "scam_suspect" (also for agents without screening)."""
    if scam.level(h.scam_flags) == "high":
        h.screen_issues = list(dict.fromkeys([*(h.screen_issues or []), "scam_suspect"]))


def _scam_prefix(h: Hit) -> str:
    return "! " if scam.warnings(h.scam_flags) else ""


def _scam_line(h: Hit) -> str:
    top = scam.headline(h.scam_flags)
    return f"\n! {top}" if top else ""


def _scam_data(h: Hit) -> dict:
    """Scam flag codes in the HA event (warn/high only), so automations can filter on them."""
    w = scam.warnings(h.scam_flags)
    return {"scam_flags": [f["code"] for f in w], "scam_level": scam.level(w)} if w else {}
