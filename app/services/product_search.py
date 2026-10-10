"""Produktsuche: a free description -> Claude plans willhaben searches -> the app runs them -> Claude picks the best.

Flow of one search (a background task, the page polls its progress):
1. plan   – ONE Claude call (purpose "search"): summary, budget, criteria and up to N keyword searches. The depth
            setting bounds N (3 / 6 / 12, hard cap MAX_QUERIES); two toggles widen the plan (unusual names, other
            categories), they never raise the cap. The same call may add up to MAX_QUESTIONS short questions
            ("Rückfragen") about things the description leaves open that matter for judging the offers (setting
            "ask", on by default).
2. search – the queries run strictly one after another through the anonymous client (get_public(), the shared
            willhaben RateLimiter + hourly budget): one request per query, nothing else. Ads are de-duplicated by
            ad id; "Suche"/"Tausch" ads, ads far above the budget and ads whose title has an exclusion word are
            dropped in code; at most MAX_CANDIDATES go on, taken round-robin so every query is represented.
            A block or an exhausted budget stops the searching; what was found so far is still ranked.
            If the plan has questions, the cards are stored and the search waits in status "asking" (no task, no
            Claude call) until the person answers or skips them (continue_run); the answers go into the ranking and
            photo prompts and into the screening brief of a search agent made from the search.
3. rank   – ONE more Claude call: the best TOP_N with a fit score, a short reason and red flags. The app adds the
            price position (deals.position against the other ads of the same query, no extra request) and the
            rule-based scam flags (scam.flags, pure function).
4. photos – optional (setting "photos", on by default), ONE more Claude call: the thumbnails of the ranked results
            (at most TOP_N, downloaded from the willhaben CDN) go to Claude with the text-stage fit and reason; it says
            whether each photo shows the item and its condition and gives the new fit, weighed against what the
            person cares about (no fixed caps; apply_photos only limits a raise). If it fails, the text-only fits
            stay.

Every search is stored in the ProductSearch table; a stored search can be re-run with its plan (no new plan call,
no new questions; the stored answers are kept).
"""
from __future__ import annotations

import asyncio
import logging
import re
import tempfile
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from sqlmodel import col, select

from app.db import ProductSearch, session, utcnow
from app.i18n import t
from app.llm.base import LLMError, get_backend
from app.llm.prompts import (
    SEARCH_PHOTO_SCHEMA,
    SEARCH_PHOTO_SYSTEM,
    SEARCH_PLAN_SCHEMA,
    SEARCH_PLAN_SYSTEM,
    SEARCH_RANK_SCHEMA,
    SEARCH_RANK_SYSTEM,
    search_photo_prompt,
    search_plan_prompt,
    search_rank_prompt,
)
from app.services import ad_images, deals, scam
from app.willhaben.parse import AdSummary, keyword_search_url
from app.willhaben.public import Blocked, WillhabenError, get_public

log = logging.getLogger(__name__)

DEPTHS = {"normal": 3, "thorough": 6, "deep": 12}   # max. queries (= willhaben requests) per depth
MAX_QUERIES = 12
ROWS = 30
SORTS = {"relevance": 7, "newest": 1}
MAX_CANDIDATES = 40
TOP_N = 10
OVER_BUDGET = 1.5            # ads above 1.5 × the budget are not worth a look
TEXT_CHARS = 220             # ad text per candidate in the ranking prompt
MAX_DESCRIPTION = 2000
RECENT = 10
RUNNING = ("planning", "searching", "ranking", "photos")    # "asking" waits for the person, no task runs
CLAUDE_CALLS = 2             # plan + ranking; the templates add one when settings["photos"] is on
MAX_PHOTOS = TOP_N           # photos sent in the photo check
PHOTO_RAISE = 10             # the photo check may raise the text fit by at most this much
PHOTO_VERDICTS = ("match", "unclear", "mismatch")
CONDITIONS = ("good", "worn", "damaged", "unclear")
MAX_QUESTIONS = 3            # clarifying questions from the plan call
MAX_OPTIONS = 4
MAX_ANSWER = 200             # chars of one answer (free text included)
_ID_RE = re.compile(r"[^a-z0-9_]+")
DEFAULT_PATH = "kaufen-und-verkaufen/marktplatz"
_PATH_RE = re.compile(r"^kaufen-und-verkaufen/marktplatz/[a-z0-9-]+-\d+$")
_LEVEL_DE = {"bargain": "Schnäppchen", "cheap": "günstig", "fair": "fair", "expensive": "teuer",
             "very_expensive": "sehr teuer"}

_transport = None             # tests inject a mock transport for the photo downloads
_tasks: set[asyncio.Task] = set()
_active: set[int] = set()     # searches with a live background task in this process


class Busy(RuntimeError):
    """Another product search is still running."""


# ------------------------------------------------------------------ settings + plan
def make_settings(depth: str | None = "normal", unusual: bool = False, categories: bool = False,
                  sort: str | None = "relevance", photos: bool = True, ask: bool = True) -> dict[str, Any]:
    return {"depth": depth if depth in DEPTHS else "normal", "unusual": bool(unusual), "categories": bool(categories),
            "sort": sort if sort in SORTS else "relevance", "photos": bool(photos), "ask": bool(ask)}


def max_queries(settings: dict[str, Any] | None) -> int:
    return min(DEPTHS.get((settings or {}).get("depth") or "normal", DEPTHS["normal"]), MAX_QUERIES)


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v) if v >= 0 else None


def _texts(v: Any, limit: int = 10, chars: int = 120) -> list[str]:
    if not isinstance(v, list):
        return []
    out = [re.sub(r"\s+", " ", x).strip()[:chars] for x in v if isinstance(x, str)]
    return [x for x in out if x][:limit]


def normalize_questions(v: Any) -> list[dict[str, Any]]:
    """Claude's clarifying questions, cleaned: slug ids (a-z0-9_, unique), one-line text (≤ 120 chars), 2 to 4
    unique options; malformed ones (no text, < 2 or > 4 options, empty or repeated id) are dropped; at most
    MAX_QUESTIONS."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for q in v if isinstance(v, list) else []:
        if not isinstance(q, dict) or not isinstance(q.get("id"), str) or not isinstance(q.get("text"), str):
            continue
        qid = _ID_RE.sub("_", q["id"].strip().lower()).strip("_")[:30]
        text = _one_line(q["text"])[:120]
        raw = q.get("options")
        if not qid or qid in seen or not text or not isinstance(raw, list) or len(raw) > MAX_OPTIONS:
            continue
        options: list[str] = []
        for o in raw:
            o = _one_line(o)[:60] if isinstance(o, str) else ""
            if o and o.lower() not in {x.lower() for x in options}:
                options.append(o)
        if len(options) < 2:
            continue
        seen.add(qid)
        out.append({"id": qid, "text": text, "options": options})
        if len(out) >= MAX_QUESTIONS:
            break
    return out


def normalize_plan(data: dict[str, Any], limit: int) -> dict[str, Any]:
    """Claude's plan, cleaned: valid numbers, short strings, unique keywords, at most `limit` (≤ 12) queries, at
    most MAX_QUESTIONS questions."""
    limit = max(1, min(limit, MAX_QUERIES))
    max_price = _num(data.get("max_price"))
    queries, seen = [], set()
    for q in data.get("queries") or []:
        if not isinstance(q, dict) or not isinstance(q.get("keyword"), str):
            continue
        kw = re.sub(r"\s+", " ", q["keyword"]).strip()[:60]
        if not kw or kw.lower() in seen:
            continue
        seen.add(kw.lower())
        pf, pt = _num(q.get("price_from")), _num(q.get("price_to"))
        if pf is not None and pt is not None and pf > pt:
            pf = None
        path = q.get("category_path") if isinstance(q.get("category_path"), str) else None
        path = path.strip().strip("/") if path else None
        hint = q.get("category_hint") if isinstance(q.get("category_hint"), str) else None
        queries.append({"keyword": kw, "price_from": pf, "price_to": pt or None,
                        "category_hint": (hint or "").strip()[:80] or None,
                        "category_path": path if path and _PATH_RE.match(path) else None,
                        "why": str(q.get("why") or "").strip()[:200]})
        if len(queries) >= limit:
            break
    return {"summary": str(data.get("summary") or "").strip()[:500], "max_price": max_price or None,
            "must_have": _texts(data.get("must_have")), "nice_to_have": _texts(data.get("nice_to_have")),
            "exclude": _texts(data.get("exclude")), "queries": queries,
            "questions": normalize_questions(data.get("questions"))}


async def make_plan(description: str, settings: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """(plan, model). Raises LLMError."""
    n = max_queries(settings)
    ask = bool(settings.get("ask"))
    prompt = search_plan_prompt(description=description, max_queries=n, unusual_names=bool(settings.get("unusual")),
                                other_categories=bool(settings.get("categories")), ask=ask)
    out = await get_backend("search").complete(system=SEARCH_PLAN_SYSTEM, prompt=prompt, schema=SEARCH_PLAN_SCHEMA)
    plan = normalize_plan(out.data or {}, n)
    if not ask:
        plan["questions"] = []
    return plan, out.model


# ------------------------------------------------------------------ questions + answers
def clean_answers(plan: dict[str, Any] | None, raw: dict[str, Any] | None) -> dict[str, str]:
    """{question id: answer} for the plan's questions only, each answer on one line (it goes into prompts) and
    short; empty answers are left out."""
    ids = {q.get("id") for q in (plan or {}).get("questions") or [] if isinstance(q, dict)}
    out = {}
    for k, v in (raw or {}).items():
        a = _one_line(v)[:MAX_ANSWER] if isinstance(v, str) else ""
        if k in ids and a:
            out[k] = a
    return out


def answer_pairs(plan: dict[str, Any] | None, answers: dict[str, Any] | None) -> list[tuple[str, str]]:
    """(question text, answer) in question order; unanswered questions are left out."""
    out = []
    for q in (plan or {}).get("questions") or []:
        if not isinstance(q, dict):
            continue
        a = (answers or {}).get(q.get("id"))
        if isinstance(a, str) and _one_line(a):
            out.append((_one_line(q.get("text"))[:120], _one_line(a)[:MAX_ANSWER]))
    return out


def preferences_text(plan: dict[str, Any] | None, answers: dict[str, Any] | None) -> str:
    """The answers as plain German lines ("Rückfragen und Antworten:" + "– question: answer"), "" without any."""
    pairs = answer_pairs(plan, answers)
    if not pairs:
        return ""
    return "\n".join(["Rückfragen und Antworten:", *[f"– {q}: {a}" for q, a in pairs]])


# ------------------------------------------------------------------ searching
def drop_reason(ad: AdSummary, plan: dict[str, Any], q: dict[str, Any] | None = None) -> str | None:
    """Why an ad is not a candidate (cheap checks in code), or None. The query's price_to is applied here,
    not sent to willhaben, so pricier ads of the same search still count as price comparables."""
    if deals._wanted(ad.heading):
        return "wanted"
    mp = plan.get("max_price")
    if mp and ad.price and ad.price > mp * OVER_BUDGET:
        return "over_budget"
    pt = (q or {}).get("price_to")
    if pt and ad.price and ad.price > pt:
        return "over_budget"
    title = (ad.heading or "").lower()
    for word in plan.get("exclude") or []:
        w = word.lower().strip()
        if w and " " not in w and re.search(rf"(?<!\w){re.escape(w)}", title):
            return "excluded"
    return None


def _url(q: dict[str, Any], sort: int) -> str:
    # no price_to: the more expensive ads stay in the comparables (drop_reason filters them as candidates)
    return keyword_search_url(q["keyword"], ROWS, q.get("price_from"), None, sort,
                              path=q.get("category_path") or DEFAULT_PATH)


def _remaining_budget() -> int | None:
    lim = get_public().limiter
    return max(0, lim.per_hour - lim.last_hour) if lim.per_hour else None


async def run_queries(plan: dict[str, Any], settings: dict[str, Any],
                      on_progress: Callable[[int, int, int], None] | None = None) -> dict[str, Any]:
    """Run the plan's queries one after another. Returns {per_query, pools, prices, requests, warning}:
    pools[i] = the new, kept ads of query i (relevance order), prices[i] = all prices of query i (comparables).
    requests counts only requests that actually went out (not those the shared limiter refused);
    on_progress(done, total, requests)."""
    queries = plan.get("queries") or []
    sort = SORTS.get(settings.get("sort") or "relevance", 7)
    per_query = [{**q, "found": None, "kept": 0, "error": None, "skipped": False} for q in queries]
    pools: list[list[AdSummary]] = [[] for _ in queries]
    prices: list[list[float]] = [[] for _ in queries]
    seen: set[str] = set()
    requests, warning = 0, None
    budget = _remaining_budget()
    if budget is not None and budget < len(queries):
        warning = t("Hourly willhaben request limit almost used up: only {n} of {total} searches run.",
                    n=budget, total=len(queries))
    client = get_public()
    stopped = False
    for i, q in enumerate(queries):
        if stopped or (budget is not None and i >= budget):
            per_query[i]["skipped"] = True
            continue
        try:
            page = await client.search(_url(q, sort))      # uncached: the count shown is the real request count
            requests += 1
        except Blocked as e:
            requests += int(getattr(e, "sent", False))      # False: refused by the limiter, nothing was sent
            per_query[i]["error"] = str(e)
            stopped = True
            msg = t("willhaben paused the search ({error}): stopped after {n} searches, the offers found so far are ranked.", error=e, n=i)
            warning = f"{warning} {msg}" if warning else msg
            continue
        except WillhabenError as e:
            requests += 1                                   # went through the limiter (network error / HTTP status)
            per_query[i]["error"] = str(e)
            continue
        finally:
            if on_progress:
                on_progress(i + 1, len(queries), requests)
        per_query[i]["found"] = page.rows_found
        for ad in page.ads:
            if ad.price and ad.price > 0 and not deals._wanted(ad.heading):
                prices[i].append(ad.price)
            if not ad.ad_id or ad.ad_id in seen:
                continue
            seen.add(ad.ad_id)
            if drop_reason(ad, plan, q) is None:
                pools[i].append(ad)
        per_query[i]["kept"] = len(pools[i])
    return {"per_query": per_query, "pools": pools, "prices": prices, "requests": requests, "warning": warning}


def pick_candidates(pools: list[list[AdSummary]], cap: int = MAX_CANDIDATES) -> list[tuple[int, AdSummary]]:
    """Round-robin over the queries (each keeps its relevance order) so every query gets a say; (query idx, ad)."""
    out: list[tuple[int, AdSummary]] = []
    depth = max((len(p) for p in pools), default=0)
    for k in range(depth):
        for i, pool in enumerate(pools):
            if k < len(pool):
                out.append((i, pool[k]))
                if len(out) >= cap:
                    return out
    return out


def _price_without(prices: list[float], price: float | None) -> list[float]:
    rest = list(prices)
    if price in rest:
        rest.remove(price)       # the ad itself is not its own comparable
    return rest


def _one_line(text: str | None) -> str:
    """Seller text collapsed to one line, so it cannot fake a new candidate entry or task line in the prompt."""
    return re.sub(r"\s+", " ", text or "").strip()


def card(qi: int, ad: AdSummary, plan: dict[str, Any], prices: list[float]) -> dict[str, Any]:
    """Everything the result list shows about one ad, price position and scam flags included (no request)."""
    comp = _price_without(prices, ad.price)
    st = deals.stats(comp)
    pos = deals.position(ad.price, st, None)
    item = {"heading": ad.heading, "body": ad.body, "price": ad.price, "is_private": ad.is_private}
    return {"ad_id": ad.ad_id, "title": ad.heading, "price": ad.price, "price_display": ad.price_display,
            "location": ad.location, "url": ad.url, "image_url": ad.image_url, "is_private": ad.is_private,
            "published_at": ad.published_at.isoformat() if ad.published_at else None,
            "text": _one_line(ad.body)[:TEXT_CHARS], "query": (plan.get("queries") or [{}])[qi].get("keyword"),
            "position": pos, "median": st["median"] if st else None, "min": st["min"] if st else None,
            "p25": st["p25"] if st else None, "comparables": st["count"] if st else 0,
            "scam_flags": scam.flags(item, scam.median_context(comp))}


# ------------------------------------------------------------------ ranking
def _rank_line(nr: int, c: dict[str, Any]) -> dict[str, Any]:
    return {"nr": nr, "title": _one_line(c["title"])[:120],
            "price": c["price_display"] or (f"{c['price']:g} €" if c["price"] is not None else None),
            "location": c["location"], "private": c["is_private"], "text": c["text"],
            "level": _LEVEL_DE.get((c.get("position") or {}).get("level") or ""),
            "scam": [f["label"] for f in scam.warnings(c["scam_flags"])]}


def merge_ranking(data: dict[str, Any], cards: list[dict[str, Any]], top_n: int = TOP_N) -> list[dict[str, Any]]:
    """Claude's picks joined with the app's cards (unknown or repeated nr ignored), best fit first."""
    out, used = [], set()
    for r in data.get("results") or []:
        if not isinstance(r, dict):
            continue
        nr = r.get("nr")
        if not isinstance(nr, int) or isinstance(nr, bool) or not 1 <= nr <= len(cards) or nr in used:
            continue
        used.add(nr)
        fit = r.get("fit")
        fit = max(0, min(100, int(fit))) if isinstance(fit, (int, float)) and not isinstance(fit, bool) else None
        out.append({**cards[nr - 1], "fit": fit, "reason": str(r.get("reason") or "").strip()[:500],
                    "red_flags": _texts(r.get("red_flags"), limit=5, chars=80)})
    out.sort(key=lambda c: -(c["fit"] if c["fit"] is not None else -1))
    return out[:top_n]


async def rank(description: str, plan: dict[str, Any], cards: list[dict[str, Any]], top_n: int = TOP_N,
               answers: list[tuple[str, str]] | None = None) -> tuple[list[dict[str, Any]], str, str]:
    """(results, note, model); answers = answer_pairs(...). Raises LLMError."""
    prompt = search_rank_prompt(description=description, plan=plan,
                                candidates=[_rank_line(nr, c) for nr, c in enumerate(cards, 1)], top_n=top_n,
                                answers=answers)
    out = await get_backend("search").complete(system=SEARCH_RANK_SYSTEM, prompt=prompt, schema=SEARCH_RANK_SCHEMA)
    return merge_ranking(out.data or {}, cards, top_n), str((out.data or {}).get("note") or "").strip()[:300], out.model


def unranked(cards: list[dict[str, Any]], top_n: int = TOP_N) -> list[dict[str, Any]]:
    """Fallback when Claude fails: cheapest relative to its own search first, no fit score."""
    def key(c: dict[str, Any]) -> float:
        p = (c.get("position") or {}).get("pos")
        return p if p is not None else 2.0
    return [{**c, "fit": None, "reason": "", "red_flags": []} for c in sorted(cards, key=key)[:top_n]]


# ------------------------------------------------------------------ photo check
def _fit(v: Any) -> int | None:
    return max(0, min(100, int(v))) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def apply_photos(results: list[dict[str, Any]], data: dict[str, Any],
                 nrs: set[int] | None = None) -> list[dict[str, Any]]:
    """Claude's photo verdicts merged into the ranked results (nr = 1-based position; unknown, repeated or not sent
    nr ignored), best fit first. The text fit is kept as fit_text; the photo may lower the fit freely but raise it
    by at most PHOTO_RAISE (a guard against inflation). There are no fixed caps for a mismatch or damage: how much
    that matters depends on the person (the prompt weighs it against the description and the answers). Results
    without a verdict keep their fit and get photo=None."""
    out = [{**r, "photo": None} for r in results]
    used: set[int] = set()
    for v in data.get("results") or []:
        if not isinstance(v, dict):
            continue
        nr = v.get("nr")
        if not isinstance(nr, int) or isinstance(nr, bool) or not 1 <= nr <= len(out) or nr in used \
                or (nrs is not None and nr not in nrs):
            continue
        used.add(nr)
        r = out[nr - 1]
        text = r.get("fit")
        photo = v.get("photo") if v.get("photo") in PHOTO_VERDICTS else "unclear"
        condition = v.get("condition") if v.get("condition") in CONDITIONS else "unclear"
        fit = _fit(v.get("fit"))
        if fit is None:
            fit = text
        elif text is not None:
            fit = min(fit, text + PHOTO_RAISE)
        r.update(fit_text=text, fit=fit, photo=photo, condition=condition,
                 photo_note=_one_line(str(v.get("photo_note") or ""))[:200])
    out.sort(key=lambda c: -(c["fit"] if c["fit"] is not None else -1))
    return out


async def check_photos(description: str, plan: dict[str, Any], results: list[dict[str, Any]],
                       answers: list[tuple[str, str]] | None = None) -> tuple[list[dict[str, Any]], str | None]:
    """(results, warning): one Claude call with the thumbnails of the ranked results. On failure the results come
    back unchanged with a warning."""
    todo = [(nr, r) for nr, r in enumerate(results, 1) if r.get("image_url")][:MAX_PHOTOS]
    if not todo:
        return results, None
    with tempfile.TemporaryDirectory(prefix="wh-search-") as tmp:
        images: list[Path] = []
        items = []
        for nr, r in todo:
            img = await ad_images.download(r["image_url"], Path(tmp), f"r{nr}", transport=_transport)
            if img is None:
                continue
            images.append(img)
            items.append({"nr": nr, "title": _one_line(r.get("title"))[:120],
                          "price": r.get("price_display") or (f"{r['price']:g} €" if r.get("price") is not None else None),
                          "level": _LEVEL_DE.get((r.get("position") or {}).get("level") or ""),
                          "fit": r.get("fit"), "reason": _one_line(r.get("reason"))[:300],
                          # the CLI wrapper copies photos in order as foto1, foto2, ...
                          "photo": f"foto{len(images)}{img.suffix}"})
        if not images:
            return results, t("Claude could not check the photos ({error}); the fit scores are text-only.",
                              error=t("no photo could be downloaded"))
        prompt = search_photo_prompt(description=description, plan=plan, items=items, answers=answers)
        try:
            out = await get_backend("search").complete(system=SEARCH_PHOTO_SYSTEM, prompt=prompt,
                                                       schema=SEARCH_PHOTO_SCHEMA, images=images)
        except LLMError as e:
            return results, t("Claude could not check the photos ({error}); the fit scores are text-only.", error=e)
    return apply_photos(results, out.data or {}, {it["nr"] for it in items}), None


# ------------------------------------------------------------------ stored searches + background task
def _update(search_id: int, **fields: Any) -> None:
    with session() as db:
        row = db.get(ProductSearch, search_id)
        if row is None:
            return
        for k, v in fields.items():
            setattr(row, k, v)
        db.add(row)
        db.commit()


def get(search_id: int) -> ProductSearch | None:
    with session() as db:
        return db.get(ProductSearch, search_id)


def recent(limit: int = RECENT) -> list[ProductSearch]:
    with session() as db:
        return list(db.exec(select(ProductSearch).order_by(col(ProductSearch.started_at).desc(),
                                                           col(ProductSearch.id).desc()).limit(limit)))


def is_running(row: ProductSearch | None) -> bool:
    return bool(row and row.status in RUNNING and row.id in _active)


def running() -> ProductSearch | None:
    for sid in sorted(_active):
        row = get(sid)
        if is_running(row):
            return row
    return None


def _spawn(coro: Coroutine[Any, Any, Any]) -> None:
    task = asyncio.get_running_loop().create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def start(description: str, settings: dict[str, Any]) -> int:
    """Store a new search and run it in the background. Raises Busy while another one runs."""
    if running():
        raise Busy(t("A product search is still running – wait for it to finish."))
    desc = (description or "").strip()[:MAX_DESCRIPTION]
    with session() as db:
        row = ProductSearch(description=desc, settings=settings, status="planning", started_at=utcnow(),
                            progress={"done": 0, "total": max_queries(settings)})
        db.add(row)
        db.commit()
        db.refresh(row)
    _active.add(row.id)
    _spawn(run(row.id))
    return row.id


def rerun(search_id: int) -> None:
    """Run a stored search again with its plan (only the ranking and the photo check go to Claude); without a plan,
    re-plan."""
    if running():
        raise Busy(t("A product search is still running – wait for it to finish."))
    row = get(search_id)
    if row is None:
        raise KeyError(search_id)
    replan = not (row.plan and row.plan.get("queries"))
    # a re-run never asks again: the stored answers stay, a search that has none gets {} (= questions skipped)
    _update(search_id, status="planning" if replan else "searching", results=None, queries=None, requests=None,
            candidates=None, note=None, warning=None, error=None, started_at=utcnow(), finished_at=None, cards=None,
            answers=row.answers if row.answers is not None else {},
            progress={"done": 0, "total": len((row.plan or {}).get("queries") or []) or max_queries(row.settings)})
    _active.add(search_id)
    _spawn(run(search_id, replan=replan))


def continue_run(search_id: int, answers: dict[str, Any] | None) -> bool:
    """Continue a search waiting in "asking" with the person's answers ({} = skipped): ranking and photo check in
    the background, from the stored cards (no willhaben request). False (nothing done) when the search is not
    waiting for answers. Raises KeyError for an unknown search, Busy while another search runs."""
    row = get(search_id)
    if row is None:
        raise KeyError(search_id)
    if row.status != "asking":
        return False
    if running():
        raise Busy(t("A product search is still running – wait for it to finish."))
    _update(search_id, status="ranking", answers=clean_answers(row.plan, answers))
    _active.add(search_id)
    _spawn(_continue(search_id))
    return True


def recover() -> None:
    """After a restart no search task is running any more (a search in "asking" has none: it stays)."""
    with session() as db:
        for row in db.exec(select(ProductSearch).where(col(ProductSearch.status).in_(RUNNING))):
            row.status, row.error, row.finished_at = "error", t("Interrupted by a restart."), utcnow()
            db.add(row)
        db.commit()


async def run(search_id: int, replan: bool = True) -> None:
    """The whole search for one stored row; every step is written back so the page can show progress."""
    _active.add(search_id)
    try:
        row = get(search_id)
        if row is None:
            return
        settings = row.settings or make_settings()
        plan = row.plan
        if replan or not plan:
            _update(search_id, status="planning")
            try:
                plan, _model = await make_plan(row.description, settings)
            except LLMError as e:
                _update(search_id, status="error", error=t("Claude could not plan the search: {error}", error=e),
                        finished_at=utcnow())
                return
            if not plan["queries"]:
                _update(search_id, status="error", plan=plan, finished_at=utcnow(),
                        error=t("Claude proposed no searches – describe the item a bit more concretely."))
                return
        n = len(plan["queries"])
        _update(search_id, status="searching", plan=plan, progress={"done": 0, "total": n})
        found = await run_queries(plan, settings,
                                  on_progress=lambda d, total, sent: _update(search_id, requests=sent,
                                                                             progress={"done": d, "total": total}))
        picked = pick_candidates(found["pools"])
        cards = [card(qi, ad, plan, found["prices"][qi]) for qi, ad in picked]
        searched = {"queries": found["per_query"], "requests": found["requests"], "candidates": len(cards),
                    "warning": found["warning"]}
        if cards and plan.get("questions") and row.answers is None and settings.get("ask"):
            # wait for the answers: the cards are kept, nothing more runs until continue_run
            _update(search_id, status="asking", cards=cards, **searched)
            return
        _update(search_id, status="ranking", **searched)
        await _rank_and_finish(search_id, row.description, plan, settings, cards, row.answers, found["warning"])
    except Exception as e:  # noqa: BLE001 - a background task must leave a readable state behind
        log.exception("product search %s failed", search_id)
        _update(search_id, status="error", error=str(e) or e.__class__.__name__, finished_at=utcnow())
    finally:
        _active.discard(search_id)


async def _continue(search_id: int) -> None:
    """The rest of a search that waited for answers: ranking + photos from the stored cards."""
    _active.add(search_id)
    try:
        row = get(search_id)
        if row is None:
            return
        await _rank_and_finish(search_id, row.description, row.plan or {}, row.settings or make_settings(),
                               list(row.cards or []), row.answers, row.warning)
    except Exception as e:  # noqa: BLE001 - a background task must leave a readable state behind
        log.exception("product search %s failed", search_id)
        _update(search_id, status="error", error=str(e) or e.__class__.__name__, finished_at=utcnow())
    finally:
        _active.discard(search_id)


async def _rank_and_finish(search_id: int, description: str, plan: dict[str, Any], settings: dict[str, Any],
                           cards: list[dict[str, Any]], answers: dict[str, Any] | None, warning: str | None) -> None:
    """Ranking (+ photo check) of the cards, then status done; the stored cards are dropped (rows stay small)."""
    pairs = answer_pairs(plan, answers)
    note = ""
    if not cards:
        results = []
    else:
        try:
            results, note, _model = await rank(description, plan, cards, answers=pairs)
        except LLMError as e:
            results = unranked(cards)
            msg = t("Claude could not rank the offers ({error}); showing the cheapest ones instead.", error=e)
            warning = f"{warning} {msg}" if warning else msg
        else:
            if results and settings.get("photos"):
                _update(search_id, status="photos")
                results, msg = await check_photos(description, plan, results, answers=pairs)
                if msg:
                    warning = f"{warning} {msg}" if warning else msg
    _update(search_id, status="done", results=results, note=note or None, warning=warning, finished_at=utcnow(),
            cards=None)
