"""Own search agents: run their searches on the sites, diff against stored hits, notify.

An agent watches one or several sites (app/platforms): either one pasted search link (`spec`), or several searches
(`queries`, each with its platform and spec) run one after another per check: the same search term on several
sites, a pasted link plus its term on other sites, or a product search's keyword searches, with the search plan's
cheap code filters (`plan`) applied to new ads. Every hit keeps the site it came from. Nothing here knows a site's
URLs or JSON; the adapters do.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any

from sqlmodel import col, select

from app.config import get_settings
from app.db import Hit, ProductSearch, SearchAgent, get_setting, session, set_setting, utcnow
from app.i18n import t
from app.services import product_search, scam, screening
from app import platforms
from app.platforms import Blocked, Listing, PlatformError, SearchSpec, ad_ref
from app.services.notify import notify

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


def _interval(platform: platforms.Platform, interval_s: int | None) -> int:
    s = get_settings()
    return max(interval_s or s.default_agent_interval_s, s.min_agent_interval_s, platform.min_interval_s)


def create_agent(source_url: str, name: str | None = None, interval_s: int | None = None) -> SearchAgent:
    """An agent from a pasted result-list link of any supported site. Raises InvalidSearchUrl."""
    p = platforms.for_url(source_url)
    return create_agent_from_spec(p.parse_search_url(source_url), name, interval_s)


def create_agent_from_spec(spec: SearchSpec, name: str | None = None, interval_s: int | None = None) -> SearchAgent:
    p = platforms.get(spec.platform)
    agent = SearchAgent(name=name or p.describe(spec), platform=p.id, spec=spec.to_json(), source_url=spec.url,
                        webapi_url=spec.api or spec.url, interval_s=_interval(p, interval_s))
    with session() as db:
        db.add(agent)
        db.commit()
        db.refresh(agent)
    return agent


def _query_of(spec: SearchSpec) -> dict[str, Any]:
    """One entry of SearchAgent.queries for a search on any site."""
    return {"keyword": spec.keyword or "", "platform": spec.platform, "spec": spec.to_json(), "webapi_url": spec.api,
            "source_url": spec.url, "price_from": spec.price_from, "price_to": spec.price_to}


def create_agent_on_sites(specs: list[SearchSpec], name: str | None = None, interval_s: int | None = None) -> SearchAgent:
    """One agent for several sites: one search per site, checked together (each site paced by its own limiter).
    The first search is the main one (name, "on <site>" link). One spec makes a plain agent."""
    if not specs:
        raise ValueError("no search")
    if len(specs) == 1:
        return create_agent_from_spec(specs[0], name, interval_s)
    first = specs[0]
    sites = [platforms.get(x.platform) for x in specs]
    agent = SearchAgent(name=name or sites[0].describe(first), platform=first.platform, spec=first.to_json(),
                        source_url=first.url, webapi_url=first.api or first.url,
                        interval_s=max(_interval(x, interval_s) for x in sites), queries=[_query_of(x) for x in specs])
    with session() as db:
        db.add(agent)
        db.commit()
        db.refresh(agent)
    return agent


def agent_platforms(agent: SearchAgent) -> list[str]:
    """The sites an agent watches, in the order of its searches (one for a plain agent)."""
    ids = [str(q.get("platform") or agent.platform or "willhaben") for q in agent.queries or [] if isinstance(q, dict)]
    return list(dict.fromkeys(ids)) or [agent.platform or "willhaben"]


def agent_spec(agent: SearchAgent) -> SearchSpec:
    """The agent's search. Rows from before `spec` existed are willhaben searches with their stored URLs: the search
    term and prices are read back from the link, the stored URLs stay what is requested."""
    if agent.spec:
        return SearchSpec.from_json(agent.spec)
    pid = agent.platform or "willhaben"
    try:
        parsed = platforms.get(pid).parse_search_url(agent.source_url)
    except (platforms.PlatformError, platforms.InvalidSearchUrl, ValueError):
        parsed = SearchSpec(platform=pid, url=agent.source_url)
    return replace(parsed, platform=pid, url=agent.source_url, api=agent.webapi_url)


def agent_searches(agent: SearchAgent) -> list[dict[str, Any]]:
    """The agent's searches as `queries` entries: its list, or its one search (a plain agent)."""
    if agent.queries:
        return [dict(q) for q in agent.queries if isinstance(q, dict)]
    return [_query_of(agent_spec(agent))]


def set_searches(agent: SearchAgent, searches: list[dict[str, Any]]) -> None:
    """Store an agent's searches; the first is the main one (its site and the "on <site>" link). A plain agent keeps
    a single search without `queries`; an agent from a product search keeps its list."""
    first = searches[0]
    agent.platform, agent.spec = first.get("platform") or agent.platform, first.get("spec")
    agent.source_url, agent.webapi_url = first["source_url"], first.get("webapi_url") or first["source_url"]
    agent.queries = list(searches) if len(searches) > 1 or agent.search_id else None   # new list: JSON sees no edits


def keyword_sites(agent: SearchAgent | None = None) -> list[platforms.Platform]:
    """Sites a search term can be sent to (switched on, keyword search), plus those the agent watches already."""
    mine = agent_platforms(agent) if agent else []
    out = []
    for p in platforms.all(enabled_only=False):
        if p.can("keyword_search") and (platforms.enabled(p.id) or p.id in mine):
            out.append(p)
    return out


def _keyword_site(pid: str) -> platforms.Platform:
    try:
        p = platforms.get(pid)
    except platforms.UnknownPlatform as e:
        raise ValueError(t("Unknown platform: {id}", id=pid)) from e
    if not platforms.enabled(p.id):
        raise ValueError(t("{name} is switched off in Settings → Platforms", name=p.name))
    if not p.can("keyword_search"):
        raise ValueError(t("{name} has no keyword search.", name=p.name))
    return p


def edit_searches(agent: SearchAgent, drop: Iterable[int] = (), sites: list[str] | None = None, add: str = "",
                  price_from: float | None = None, price_to: float | None = None) -> bool:
    """Change an agent's searches in one go: remove some (indices into agent_searches), watch `sites` (None = as now;
    a newly chosen site gets every search term, a dropped one loses its searches) and add the term `add` on each of
    its sites. Raises ValueError and changes nothing on a mistake. True when searches were added: their first check
    should count what they find as seen, not push it."""
    drop = set(drop)
    kept = [q for i, q in enumerate(agent_searches(agent)) if i not in drop]
    if not kept:
        raise ValueError(t("Keep at least one search, or delete the agent."))

    def site(q: dict[str, Any]) -> str:
        return str(q.get("platform") or agent.platform or "willhaben")

    current = list(dict.fromkeys(site(q) for q in kept))
    wanted = current if sites is None else list(dict.fromkeys(s.strip().lower() for s in sites if s.strip()))
    if not wanted:
        raise ValueError(t("Choose at least one site."))
    add = " ".join((add or "").split())
    terms = list(dict.fromkeys((q["keyword"], q.get("price_from"), q.get("price_to")) for q in kept if q.get("keyword")))
    new_term = [(add, price_from, price_to)] if add else []
    searches = [q for q in kept if site(q) in wanted]
    have = {(site(q), q.get("keyword"), q.get("price_from"), q.get("price_to")) for q in searches}
    added = False
    for pid in wanted:
        todo = list(dict.fromkeys(terms + new_term)) if pid not in current else new_term
        if pid not in current and not todo:
            raise ValueError(t("The searches have no search term to use on {name}.", name=_keyword_site(pid).name))
        p = _keyword_site(pid) if todo else None
        for kw, pf, pt in todo:
            spec = p.keyword_search(kw, pf, pt)
            if (pid, spec.keyword, pf, pt) not in have:
                have.add((pid, spec.keyword, pf, pt))
                searches.append(_query_of(spec))
                added = True
    set_searches(agent, searches)
    return added


def agent_interval(agent: SearchAgent, interval_s: int) -> int:
    """`interval_s` raised to the floors: the settings' minimum and the slowest site the agent watches."""
    floors = [get_settings().min_agent_interval_s]
    for pid in agent_platforms(agent):
        try:
            floors.append(platforms.get(pid).min_interval_s)
        except platforms.UnknownPlatform:
            continue
    return max(interval_s, *floors)


def _query_spec(q: dict[str, Any], platform: str = "willhaben") -> SearchSpec:
    if q.get("spec"):
        return SearchSpec.from_json(q["spec"])
    return SearchSpec(platform=q.get("platform") or platform, url=q.get("source_url") or q.get("webapi_url") or "",
                      api=q.get("webapi_url"), keyword=q.get("keyword"), price_from=q.get("price_from"))


def agent_queries(plan: dict[str, Any] | None, platform_id: str = "willhaben") -> list[dict[str, Any]]:
    """The agent's searches for a product-search plan on one platform: newest first (agents diff on newness), no
    price_to in the search (drop_reason applies it, like the product search does)."""
    p = platforms.get(platform_id)
    out = []
    for q in (plan or {}).get("queries") or []:
        if not isinstance(q, dict) or not q.get("keyword"):
            continue
        category = q.get("category_path") if p.id == "willhaben" else None
        spec = p.keyword_search(q["keyword"], q.get("price_from"), None, category=category, sort="newest")
        out.append({"keyword": q["keyword"], "platform": p.id, "spec": spec.to_json(), "webapi_url": spec.api,
                    "source_url": spec.url, "price_from": q.get("price_from"), "price_to": q.get("price_to"),
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
                             screen: bool = True, platform: str = "willhaben", sites: list[str] | None = None) -> SearchAgent:
    """A search agent that keeps running a finished product search's keyword searches, on one platform or on
    several (`sites`: one agent, every planned search on each site). Raises KeyError for an unknown search,
    ValueError when it has no planned searches or no chosen site can search by keyword. Does not run the baseline
    (the caller schedules)."""
    with session() as db:
        row = db.get(ProductSearch, search_id)
    if row is None:
        raise KeyError(search_id)
    chosen = [platforms.get(pid) for pid in dict.fromkeys(sites or [platform])]
    usable = [x for x in chosen if x.can("keyword_search")]
    if not usable:
        raise ValueError(t("{name} has no keyword search.", name=chosen[0].name))
    per_site = [agent_queries(row.plan, x.id) for x in usable]
    queries = [q for qs in per_site for q in qs]
    if not queries:
        raise ValueError(t("This search has no planned searches yet."))
    # each site has its own limiter: the interval follows the searches per site, not their sum
    interval = max(_interval(x, interval_s or default_interval_s(max(len(qs) for qs in per_site))) for x in usable)
    p = usable[0]
    first = queries[0]
    agent = SearchAgent(name=(name or "").strip() or agent_name(row), platform=p.id, spec=first["spec"],
                        source_url=first["source_url"], webapi_url=first["webapi_url"] or first["source_url"],
                        interval_s=interval, queries=queries, plan=agent_plan(row.plan), search_id=row.id)
    if screen:
        agent.screen, agent.screen_brief, agent.screen_min = True, screen_brief(row) or None, "match"
    with session() as db:
        db.add(agent)
        db.commit()
        db.refresh(agent)
    return agent


async def _search_all(specs: list[tuple[SearchSpec, dict[str, Any] | None]]) -> tuple[
        list[tuple[Listing, dict[str, Any] | None]], int, list[str], Exception | None, int]:
    """Run an agent's searches one after another (uncached), each on its platform. Returns (ads with their query,
    de-duplicated by ad id, first occurrence wins; summed rows_found; error lines; first exception; number of
    searches that succeeded). A block stops the remaining searches of this run."""
    ads: list[tuple[Listing, dict[str, Any] | None]] = []
    seen: set[tuple[str, str]] = set()
    rows, errors, first_exc, ok = 0, [], None, 0
    n = len(specs)
    for i, (spec, q) in enumerate(specs, 1):
        try:
            page = await platforms.get(spec.platform).search(spec)
        except PlatformError as e:
            log.warning("agent search %s/%s (%s) failed: %s", i, n, spec.keyword, e)
            errors.append(t("search {i}/{n} failed: {error}", i=i, n=n, error=e) if n > 1 else str(e))
            first_exc = first_exc or e
            if isinstance(e, Blocked):
                if i < n:
                    errors.append(t("remaining searches skipped: {k}", k=n - i))
                break
            continue
        ok += 1
        rows += page.rows_found or 0
        for ad in page.ads:
            ad.platform = spec.platform               # the hit belongs to the site that was searched
            if not ad.ad_id or (spec.platform, ad.ad_id) in seen:
                continue
            seen.add((spec.platform, ad.ad_id))
            ads.append((ad, q))
    return ads, rows, errors, first_exc, ok


def _apply(hit: Hit, ad: Listing) -> None:
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
        name, queries, plan = agent.name, agent.queries, agent.plan
        specs = [(_query_spec(q, agent.platform), q) for q in queries] if queries else [(agent_spec(agent), None)]

    def _fail(error: str) -> RunResult:
        with session() as db:
            row = db.get(SearchAgent, agent_id)
            row.last_run_at, row.last_error = utcnow(), error
            db.add(row)
            db.commit()
        res.error = error
        return res

    usable, skipped = [], []                 # searches on unknown or switched-off sites are skipped (and said so)
    for spec, q in specs:
        try:
            site = platforms.get(spec.platform)
        except platforms.UnknownPlatform:
            skipped.append(t("Unknown platform: {id}", id=spec.platform))
            continue
        if not platforms.enabled(site.id):
            skipped.append(t("{name} is switched off in Settings → Platforms", name=site.name))
            continue
        usable.append((spec, q))
    skipped = list(dict.fromkeys(skipped))
    if not usable:
        return _fail("; ".join(skipped))
    found, rows_found, errors, first_exc, ok = await _search_all(usable)
    if not ok:
        return _fail(str(first_exc))         # every search failed (one search: its error)
    partial_error = "; ".join(skipped + errors) or None

    res.rows_found = rows_found
    with session() as db:
        agent = db.get(SearchAgent, agent_id)
        res.baseline = not agent.baseline_done
        existing = {((h.platform or "willhaben"), h.ad_id): h for h in db.exec(select(Hit).where(Hit.agent_id == agent_id))}
        id_site = {ad_id: pid for pid, ad_id in existing}     # Hit is unique per (agent, ad id), whatever the site
        scam_ctx = _scam_context(existing.values(), [ad for ad, _ in found])
        for ad, query in found:
            if not ad.ad_id:
                continue
            hit = existing.get((ad.platform, ad.ad_id))
            if hit is None and id_site.get(ad.ad_id, ad.platform) != ad.platform:
                log.warning("agent %s: %s ad %s has the id of a %s hit, skipped", agent_id, ad.platform, ad.ad_id,
                            id_site[ad.ad_id])
                continue
            if hit is None:
                if plan and product_search.drop_reason(ad, plan, query):
                    continue        # "Suche" ad, far over budget or an exclusion word: never stored or notified
                hit = Hit(agent_id=agent_id, platform=ad.platform, ad_id=ad.ad_id, heading=ad.heading, url=ad.url,
                          state="seen" if res.baseline else "new")
                _apply(hit, ad)
                hit.scam_flags = scam.flags(hit, scam_ctx, home_country=_site(ad.platform).home_country) or None   # before screening
                _mark_scam_issue(hit)
                db.add(hit)
                existing[(ad.platform, ad.ad_id)] = hit
                id_site[ad.ad_id] = ad.platform
                if not res.baseline:
                    res.new.append(hit)
                continue
            prev_price, prev_published = hit.price, hit.published_at
            _apply(hit, ad)
            if prev_price is not None and ad.price is not None and ad.price < prev_price:
                hit.price_dropped_at = utcnow()
                hit.old_price = prev_price
                hit.scam_flags = scam.flags(hit, scam_ctx, home_country=_site(hit.platform).home_country) or None   # a new red flag?
                _mark_scam_issue(hit)
                if hit.state != "ignored":
                    res.price_drops.append(hit)
            if ad.is_bumped is True and prev_published and ad.published_at and ad.published_at > prev_published:
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

    # the site in the title once agents watch more than one; always in the payload (HA automations, app links)
    multi = platforms.multi()

    def label(pid: str) -> str:
        return f"{_site(pid).name} · {name}" if multi else name

    if notify_new and res.new:
        good = [h.id for h in res.new if screening.passes(agent, h)]
        pushed = set(good[:MAX_PUSH_PER_RUN])
        for h in res.new:
            if h.id in pushed or h.id not in good:   # rejected hits: HA event only, no push
                await notify("hit", _scam_prefix(h) + f"{label(h.platform)}: {h.price_display or ''}".strip(" :"),
                             _hit_text(h), url=h.url, image=h.image_url, push=h.id in pushed,
                             data={"agent": name, "platform": h.platform, "ad_id": h.ad_id, "ref": ad_ref(h.platform, h.ad_id),
                                   "price": h.price, "location": h.location, **_screen_data(h), **_scam_data(h)})
        if len(good) > MAX_PUSH_PER_RUN:
            sites = list(dict.fromkeys(h.platform for h in res.new))
            await notify("hit", label(sites[0]) if len(sites) == 1 else name,
                         t("+{n} more new hits", n=len(good) - MAX_PUSH_PER_RUN),
                         data={"agent": name, "platform": sites[0] if len(sites) == 1 else None, "count": len(good)})
    if notify_drops:
        for h in res.price_drops[:MAX_PUSH_PER_RUN]:
            await notify("price_drop", _scam_prefix(h) + t("{name}: price dropped", name=label(h.platform)),
                         f"{h.heading}: {h.old_price:g} → {h.price:g} {_site(h.platform).currency}" + _scam_line(h)
                         + _screen_line(h), url=h.url, image=h.image_url, push=screening.passes(agent, h),
                         data={"agent": name, "platform": h.platform, "ad_id": h.ad_id, "ref": ad_ref(h.platform, h.ad_id),
                               "price": h.price, "old_price": h.old_price, **_screen_data(h), **_scam_data(h)})
    return res


def _site(pid: str | None) -> platforms.Platform:
    """The adapter of a hit's site; willhaben's when that site is gone (wording only: name, currency, country)."""
    try:
        return platforms.get(pid or "willhaben")
    except platforms.UnknownPlatform:
        return platforms.get("willhaben")


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
