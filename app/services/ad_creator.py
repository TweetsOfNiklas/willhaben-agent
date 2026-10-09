"""Ad drafting: notes + photos -> Claude writes the listing; price hint from comparable ads."""
from __future__ import annotations

import io
import logging
import re
import statistics
import zipfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.db import AdDraft, get_setting, session, utcnow
from app.i18n import t
from app.llm.base import LLMError, get_backend
from app.llm.prompts import AD_SCHEMA, AD_SYSTEM, ad_prompt, ad_refine_prompt
from app.services import geizhals
from app.willhaben.public import WillhabenError, get_public

log = logging.getLogger(__name__)

MAX_IMAGES = 8
MAX_IMAGE_BYTES = 15 * 1024 * 1024
ALLOWED = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
INSERT_URL = "https://www.willhaben.at/iad/anzeigenaufgabe"


def nice_price(v: float) -> float:
    step = 1 if v < 20 else 5 if v < 100 else 10 if v < 1000 else 50
    return max(step, round(v / step) * step)


def price_stats(prices: list[float]) -> dict[str, Any] | None:
    prices = sorted(p for p in prices if p and p > 0)
    if len(prices) < 3:
        return None
    q = statistics.quantiles(prices, n=4)
    return {"count": len(prices), "median": statistics.median(prices), "p25": q[0], "p75": q[2],
            "min": prices[0], "max": prices[-1]}


def save_images(draft_id: int, files: list[tuple[str, bytes]]) -> list[str]:
    folder = get_settings().uploads_dir / str(draft_id)
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, (name, data) in enumerate(files[:MAX_IMAGES], 1):
        ext = Path(name or "").suffix.lower()
        if ext not in ALLOWED or not data or len(data) > MAX_IMAGE_BYTES:
            continue
        p = folder / f"{i:02d}{ext}"
        p.write_bytes(data)
        paths.append(str(p))
    return paths


async def create_draft(notes: str, files: list[tuple[str, bytes]], batch_id: int | None = None) -> AdDraft:
    with session() as db:
        draft = AdDraft(input_text=notes, batch_id=batch_id)
        db.add(draft)
        db.commit()
        db.refresh(draft)
    paths = save_images(draft.id, files)
    try:
        result = await get_backend("ads").complete(
            system=AD_SYSTEM, prompt=ad_prompt(notes, get_setting("reply_rules"), len(paths)),
            schema=AD_SCHEMA, images=[Path(p) for p in paths])
    except LLMError as e:
        with session() as db:
            d = db.get(AdDraft, draft.id)
            d.image_paths, d.status, d.error = paths, "error", str(e)
            db.add(d)
            db.commit()
            db.refresh(d)
        return d

    data = result.data
    stats, comparables = None, []
    keywords = (data.get("search_keywords") or "").strip()
    if keywords:
        try:
            page = await get_public().search_keyword(keywords, rows=50, sort=7)
            stats = price_stats([a.price for a in page.ads if a.price])
            if stats:
                stats["rows_found"] = page.rows_found
            comparables = [a.as_dict() for a in page.ads if a.price][:12]
        except WillhabenError as e:
            log.info("comparable search failed: %s", e)
    suggestion = nice_price(stats["median"]) if stats else data.get("price_estimate")
    product_query = (data.get("product_query") or "").strip()
    gh = await _geizhals_lookup(product_query)

    with session() as db:
        d = db.get(AdDraft, draft.id)
        d.image_paths = paths
        d.title, d.description = data.get("title"), data.get("description")
        d.category_hint, d.condition = data.get("category_hint"), data.get("condition")
        d.attributes = data.get("attributes") or []
        d.search_keywords = keywords
        d.price_suggestion = suggestion
        d.price_stats = {**(stats or {}), "llm_estimate": data.get("price_estimate"), "note": data.get("note")}
        d.comparables = comparables
        d.model = result.model
        d.product_query, d.geizhals = product_query or None, gh
        db.add(d)
        db.commit()
        db.refresh(d)
    if ((gh or {}).get("match") or {}).get("specs"):
        d = await reconcile_with_geizhals(d.id)
    return d


def update_draft(draft_id: int, **fields: Any) -> AdDraft:
    with session() as db:
        d = db.get(AdDraft, draft_id)
        for k, v in fields.items():
            setattr(d, k, v)
        db.add(d)
        db.commit()
        db.refresh(d)
    return d


def parse_price(text: str | None) -> float | None:
    try:
        return float(text.replace(",", ".")) if (text or "").strip() else None
    except ValueError:
        return None


def save_fields(draft_id: int, title: str, description: str, price: str | None) -> AdDraft:
    """The user's edits of title, description and price (form values)."""
    return update_draft(draft_id, title=title.strip(), description=description.replace("\r\n", "\n").strip(),
                        price_suggestion=parse_price(price))


def photos_zip(d: AdDraft) -> bytes:
    """All photos of a draft, named after the title, for uploading them on willhaben by hand."""
    stem = re.sub(r"[^\w-]+", "-", (d.title or f"draft-{d.id}").lower()).strip("-")[:40] or f"draft-{d.id}"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:   # photos are compressed already
        for i, path in enumerate(d.image_paths, 1):
            if Path(path).exists():
                z.write(path, f"{stem}-{i:02d}{Path(path).suffix}")
    return buf.getvalue()


async def _geizhals_lookup(query: str) -> dict[str, Any] | None:
    if not query:
        return None
    try:
        return await geizhals.lookup(query)
    except geizhals.GeizhalsError as e:
        log.info("geizhals lookup failed for %r: %s", query, e)
        return {"query": query, "match": None, "candidates": [], "error": str(e)}


async def choose_geizhals(draft_id: int, url: str) -> AdDraft:
    """User picked another Geizhals hit (or pasted a product link)."""
    prod = await geizhals.product(url)
    with session() as db:
        d = db.get(AdDraft, draft_id)
        gh = dict(d.geizhals or {"query": d.product_query, "candidates": []})
        gh["match"] = asdict(prod)
        gh.pop("error", None)
        d.geizhals = gh
        db.add(d)
        db.commit()
        db.refresh(d)
    return d


async def search_geizhals(draft_id: int, query: str) -> AdDraft:
    gh = await _geizhals_lookup(query)
    return update_draft(draft_id, product_query=query.strip() or None, geizhals=gh)


_VERSION_FIELDS = ("title", "description", "category_hint", "condition", "attributes")

RECONCILE_INSTRUCTION = (
    "Gleiche das Inserat mit den Herstellerdaten von Geizhals ab: Korrigiere Angaben, die dem Datenblatt "
    "widersprechen, und entferne „(bitte prüfen)“ bei allem, was das Datenblatt bestätigt. Angaben, die das "
    "Datenblatt nicht klärt (z. B. Akku, Zustand), bleiben als normaler Satz mit „(bitte prüfen)“ stehen. "
    "Entferne eine selbst geschriebene Liste technischer Daten aus der Beschreibung, das Datenblatt hänge ich an."
)


async def reconcile_with_geizhals(draft_id: int) -> AdDraft:
    return await refine_draft(draft_id, RECONCILE_INSTRUCTION, label=t("Checked against the Geizhals spec sheet"))


async def refine_draft(draft_id: int, instruction: str, label: str | None = None) -> AdDraft:
    """Let Claude revise the draft; the previous version goes to `revisions` for undo."""
    with session() as db:
        d = db.get(AdDraft, draft_id)
    match = (d.geizhals or {}).get("match") or {}
    block = geizhals.spec_block(match)
    had_block = bool(block) and block in (d.description or "")
    current = {k: getattr(d, k) for k in _VERSION_FIELDS}
    if had_block:   # Claude rewrites the prose only; the spec sheet is re-attached unchanged
        current["description"] = (d.description or "").replace(block, "").rstrip()
    prompt = ad_refine_prompt(notes=d.input_text, rules=get_setting("reply_rules"), n_images=len(d.image_paths),
                              current=current, specs=match.get("specs"), product_name=match.get("name"),
                              instruction=instruction)
    try:
        result = await get_backend("ads").complete(system=AD_SYSTEM, prompt=prompt, schema=AD_SCHEMA,
                                                   images=[Path(p) for p in d.image_paths])
    except LLMError as e:
        return update_draft(draft_id, error=t("Revision failed: {error}", error=e))
    data = result.data
    description = (data.get("description") or "").rstrip()
    if had_block:
        description = f"{description}\n\n{block}"
    revisions = list(d.revisions or [])
    revisions.append({**{k: getattr(d, k) for k in _VERSION_FIELDS}, "instruction": label or instruction,
                      "at": utcnow().isoformat()})
    return update_draft(draft_id, title=data.get("title"), description=description,
                        category_hint=data.get("category_hint"), condition=data.get("condition"),
                        attributes=data.get("attributes") or [], revisions=revisions[-20:], model=result.model,
                        error=None, price_stats={**(d.price_stats or {}), "note": data.get("note")})


def undo_revision(draft_id: int) -> AdDraft:
    with session() as db:
        d = db.get(AdDraft, draft_id)
    revisions = list(d.revisions or [])
    if not revisions:
        return d
    last = revisions.pop()
    return update_draft(draft_id, revisions=revisions, **{k: last.get(k) for k in _VERSION_FIELDS})


# ------------------------------------------------------------------ publish on willhaben (direct, per click)
def category_queries(title: str, product_query: str | None, search_keywords: str | None,
                     user_query: str | None = None) -> list[str]:
    """What to ask willhaben's category suggestion, best first. Long titles often get no suggestion at all,
    and the short search keywords alone can point somewhere odd (live: "Keyboards" for a speaker)."""
    words = [w.strip(",;:·-") for w in (title or "").split()]
    cands = [user_query, title, " ".join(words[:4]), " ".join(words[:3]), product_query, search_keywords]
    out: dict[str, str] = {}
    for q in cands:
        if q and q.strip():
            out.setdefault(q.strip()[:80].lower(), q.strip()[:80])    # same query in other case: ask once
    return list(out.values())


async def prepare_willhaben(draft_id: int, query: str | None = None) -> AdDraft:
    """Read-only: category suggestions and required fields from willhaben's own form, stored for review."""
    from app.willhaben import insert
    from app.willhaben.session import get_session

    with session() as db:
        d = db.get(AdDraft, draft_id)
    sess = get_session()
    form = await insert.cached_form(sess)
    raw, used = None, None
    for q in category_queries(d.title or d.input_text, d.product_query, d.search_keywords, query):
        raw, used = await insert.suggest_categories(sess, q), q
        if insert.suggestion_paths(raw):
            break
    plan = insert.build_plan(form, raw, condition=d.condition, attributes=d.attributes)
    plan["for_title"], plan["query"] = d.title, used
    old = d.wh_plan or {}
    have = {o["path"][-1]["code"] for o in plan["options"] if o.get("path")}
    plan["options"] += [o for o in old.get("options") or [] if o.get("manual") and o["path"][-1]["code"] not in have]
    if old.get("shipping"):
        plan["shipping"] = old["shipping"]
    return update_draft(draft_id, wh_plan=plan, error=None)


async def add_manual_category(draft_id: int, code: str) -> AdDraft:
    """A category the user picked in "Choose another category": stored as option in wh_plan and pre-selected,
    so publish_willhaben can use it like a suggested one."""
    from app.willhaben import insert
    from app.willhaben.session import get_session

    with session() as db:
        d = db.get(AdDraft, draft_id)
    form = await insert.cached_form(get_session())
    opt = insert.manual_option(form, code, condition=d.condition, attributes=d.attributes)
    if opt is None:
        raise ValueError(t("This category cannot be chosen (not found or not a final category)"))
    plan = dict(d.wh_plan or {})
    if "location" not in plan:                   # picked before (or without) "Prepare for willhaben"
        base = insert.build_plan(form, None, condition=d.condition, attributes=d.attributes)
        plan.update(location=base["location"], contact=base["contact"], for_title=d.title)
    options = list(plan.get("options") or [])
    idx = next((i for i, o in enumerate(options) if o.get("path") and o["path"][-1]["code"] == code), None)
    if idx is None:
        options.append(opt)
        idx = len(options) - 1
    plan["options"], plan["selected"] = options, idx
    return update_draft(draft_id, wh_plan=plan, error=None)


def parse_shipping(form: Any) -> dict[str, Any]:
    """Übergabe fields of the publish form → {"handover", "paylivery", "size", "carrier"}; default pickup.
    Everything but pickup NEEDS LIVE VERIFICATION (insert.set_delivery)."""
    from app.willhaben import insert

    handover = str(form.get("handover") or "pickup")
    if handover not in insert.HANDOVERS:
        handover = "pickup"
    if handover == "pickup":
        return {"handover": "pickup"}
    size = str(form.get("parcel_size") or "").upper()
    carrier = str(form.get("parcel_carrier") or "")
    return {"handover": handover, "paylivery": bool(form.get("paylivery")),
            "size": size if size in insert.PARCEL_SIZES else None,
            "carrier": carrier if carrier in insert.PAYLIVERY_CARRIERS else None}


_publishing: set[int] = set()     # drafts with a publish in flight (double click, second tab)


async def publish_willhaben(draft_id: int, option: int, values: dict[str, str], stop_after: str | None = None,
                            shipping: dict[str, Any] | None = None):
    """Publish the saved draft as free private ad: the chosen category option and field values from the form.
    shipping: parse_shipping() result; None = pickup only (the live-verified path)."""
    from app.willhaben import insert

    if draft_id in _publishing:
        raise insert.InsertError("Wird gerade veröffentlicht – bitte warten")
    _publishing.add(draft_id)
    try:
        return await _publish_willhaben(draft_id, option, values, stop_after, shipping)
    finally:
        _publishing.discard(draft_id)


async def _publish_willhaben(draft_id: int, option: int, values: dict[str, str], stop_after: str | None,
                             shipping: dict[str, Any] | None):
    from app.willhaben import insert
    from app.willhaben.session import get_session

    with session() as db:
        d = db.get(AdDraft, draft_id)
    if d.status == "published":
        raise insert.InsertError("Schon veröffentlicht")
    plan = d.wh_plan or {}
    opts = plan.get("options") or []
    if not (0 <= option < len(opts)):
        raise insert.InsertError("Bitte zuerst eine Kategorie wählen")
    if not (d.title or "").strip() or not (d.description or "").strip():
        raise insert.InsertError("Titel und Beschreibung dürfen nicht leer sein")
    chosen = opts[option]
    attrs, labels = {}, {}
    for f in chosen["fields"]:
        code = values.get(f["code"]) or f.get("selected")
        allowed = dict(f["values"])
        if code not in allowed:
            raise insert.InsertError(f"Pflichtfeld „{f['label']}“ fehlt")
        attrs[f["code"]], labels[f["code"]] = (code, allowed[code]), f["label"]

    shipping = shipping if shipping and shipping.get("handover") in insert.HANDOVERS else {"handover": "pickup"}
    if shipping.get("paylivery") and not shipping.get("size"):
        raise insert.InsertError("Bitte eine Paketgröße für PayLivery wählen")
    if shipping != plan.get("shipping") or plan.get("selected") != option:   # remembered for the form
        update_draft(draft_id, wh_plan={**plan, "shipping": shipping, "selected": option})

    sess = get_session()
    form = await insert.load_form(sess)          # fresh template (contact, location, context links)
    insert.remember_form(form)
    ad = insert.compose_ad(form.get("ad") or {}, heading=d.title, description=d.description,
                           price=d.price_suggestion, path=chosen["path"], attrs=attrs, attr_labels=labels,
                           handover=shipping["handover"])
    photos = []
    for p in d.image_paths[:insert.MAX_PHOTOS]:
        path = Path(p)
        mime = {".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}.get(path.suffix.lower(), "image/jpeg")
        photos.append((path.name, path.read_bytes(), mime))

    def remember(wh_id: str) -> None:            # so a retry continues the same willhaben draft
        update_draft(draft_id, wh_ad_id=wh_id)

    res = await insert.publish(sess, ad, photos, existing_ad_id=d.wh_ad_id, on_presaved=remember, stop_after=stop_after,
                               shipping=shipping if shipping["handover"] != "pickup" else None)
    if res.get("stopped"):                       # step-by-step trial: nothing public yet, draft stays a draft
        return res
    d = update_draft(draft_id, status="published", wh_ad_id=res["ad_id"], error=None)
    from app.db import MyAd
    from app.services import my_ads
    with session() as db:                        # show up under "my ads" right away, status unknown yet
        if db.get(MyAd, res["ad_id"]) is None:
            db.add(MyAd(ad_id=res["ad_id"], heading=d.title, url=res["url"], status="unknown", image_url=None))
            db.commit()
    try:                                         # willhaben's own status (new ads are reviewed before going online)
        await my_ads.import_from_account()
    except Exception as e:  # noqa: BLE001 - the ad is published either way
        log.info("my-ads refresh after publishing failed: %s", e)
    log.info("draft %s published on willhaben as %s", draft_id, res["ad_id"])
    return d
