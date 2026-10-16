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

from sqlmodel import select

from app.config import get_settings
from app.db import AdDraft, get_setting, session, utcnow
from app.i18n import t
from app.llm.base import LLMError, get_backend
from app.llm.prompts import AD_SCHEMA, AD_SYSTEM, ad_prompt, ad_refine_prompt
from app.services import geizhals
from app.willhaben.public import WillhabenError, get_public
from app.services import timeline
from app.services.shipping import clean_parcel

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


MAX_QUANTITY = 999


def parse_quantity(value: Any) -> int | None:
    """Pieces from a form field or from Claude: a whole number 1..999; blank, text and zero are None."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    try:
        n = int(str(value).strip())
    except ValueError:
        return None
    return min(n, MAX_QUANTITY) if n >= 1 else None


def pick_quantity(owner: int | None, claude: Any) -> int | None:
    """What the draft offers: the owner's count wins; Claude's only fills in when the owner gave none and it saw more
    than one piece. One piece is no stock (None = a single item, as on My ads)."""
    n = owner if owner is not None else parse_quantity(claude)
    return n if n and n > 1 else None


def claude_parcel(raw: Any, title: str | None) -> dict[str, Any] | None:
    """Claude's packed-size estimate (for one piece) as a shipping parcel; source "claude" keeps it an estimate
    that the owner has to confirm before anything is bought. All zeros = could not estimate = None."""
    if not isinstance(raw, dict):
        return None
    return clean_parcel({**raw, "contents": (title or "")[:60], "source": "claude"})


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


@timeline.scoped(batch_id="batch_id")
async def create_draft(notes: str, files: list[tuple[str, bytes]], batch_id: int | None = None,
                       quantity: int | None = None) -> AdDraft:
    """quantity: the owner's piece count (the batch review); it wins over what Claude reads from notes and photos."""
    owner_quantity = parse_quantity(quantity)
    with session() as db:
        draft = AdDraft(input_text=notes, batch_id=batch_id, quantity=pick_quantity(owner_quantity, None))
        db.add(draft)
        db.commit()
        db.refresh(draft)
    timeline.update_context(draft_id=draft.id)
    paths = save_images(draft.id, files)
    try:
        result = await get_backend("ads").complete(
            system=AD_SYSTEM, prompt=ad_prompt(notes, get_setting("reply_rules"), len(paths), quantity=owner_quantity),
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
        d.quantity = pick_quantity(owner_quantity, data.get("quantity"))
        d.parcel = claude_parcel(data.get("parcel"), d.title)
        d.search_keywords = keywords
        d.price_suggestion = suggestion
        d.price_stats = {**(stats or {}), "llm_estimate": data.get("price_estimate"), "note": data.get("note")}
        d.comparables = comparables
        d.model = result.model
        d.product_query, d.geizhals = product_query or None, gh
        db.add(d)
        db.commit()
        db.refresh(d)
    if (gh or {}).get("verified") and ((gh or {}).get("match") or {}).get("specs"):
        # only a confirmed product/variant: a guessed one (e.g. the 8GB model for a 4GB ad) would rewrite the text
        d = await reconcile_with_geizhals(d.id)
        d = attach_specs_if_wanted(d.id)
    return d


def auto_specs() -> bool:
    """Setting: always attach the Geizhals spec sheet to the description when a product was found."""
    return get_setting("geizhals_auto_specs") == "1"


def attach_specs_if_wanted(draft_id: int, force: bool = False) -> AdDraft:
    with session() as db:
        d = db.get(AdDraft, draft_id)
    block = geizhals.spec_block((d.geizhals or {}).get("match"))
    if not block or not (force or auto_specs()) or block in (d.description or ""):
        return d
    return update_draft(draft_id, description=f"{(d.description or '').rstrip()}\n\n{block}")


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


REFINING_BUSY = "Claude is still revising this draft – save or publish again when it is done"


def save_fields(draft_id: int, title: str, description: str, price: str | None,
                quantity: str | None = None) -> AdDraft:
    """The user's edits of title, description, price and piece count (form values; quantity None = the form did not
    send it, blank = one item). Refused while Claude revises the draft: its result would overwrite the edit (the
    revision started from the text before it)."""
    with session() as db:
        d = db.get(AdDraft, draft_id)
    if d is not None and d.refining:
        return update_draft(draft_id, error=t(REFINING_BUSY))
    extra = {} if quantity is None else {"quantity": pick_quantity(parse_quantity(quantity), None)}
    return update_draft(draft_id, title=title.strip(), description=description.replace("\r\n", "\n").strip(),
                        price_suggestion=parse_price(price), **extra)


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
        old_block = geizhals.spec_block(gh.get("match"))
        had_block = bool(old_block) and old_block in (d.description or "")
        if had_block:                      # the sheet of the previous product goes; the new one replaces it
            d.description = (d.description or "").replace(old_block, "").rstrip()
        gh["match"], gh["verified"] = asdict(prod), True      # the user's own pick counts as confirmed
        gh.pop("error", None)
        d.geizhals = gh
        db.add(d)
        db.commit()
        db.refresh(d)
    return attach_specs_if_wanted(draft_id, force=had_block)


async def search_geizhals(draft_id: int, query: str) -> AdDraft:
    """User typed a product name: confirmed (`verified`) only if a hit matches it, variant included."""
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


@timeline.scoped(draft_id="draft_id")
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
    # an unconfirmed variant's sheet is no "reliable" data for a plain revision; the explicit check still uses it
    trusted = (d.geizhals or {}).get("verified") is not False or instruction == RECONCILE_INSTRUCTION
    prompt = ad_refine_prompt(notes=d.input_text, rules=get_setting("reply_rules"), n_images=len(d.image_paths),
                              current=current, specs=match.get("specs") if trusted else None,
                              product_name=match.get("name"), instruction=instruction, quantity=d.quantity)
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
    return {"handover": handover, "paylivery": str(form.get("paylivery") or "").lower() in ("1", "true", "on", "yes"),
            "size": size if size in insert.PARCEL_SIZES else None,
            "carrier": carrier if carrier in insert.PAYLIVERY_CARRIERS else None}


_publishing: set[int] = set()     # drafts with a publish in flight (double click, second tab)


async def publish_willhaben(draft_id: int, option: int, values: dict[str, str], stop_after: str | None = None,
                            shipping: dict[str, Any] | None = None, job: Any = None):
    """Publish the saved draft as free private ad: the chosen category option and field values from the form.
    shipping: parse_shipping() result; None = pickup only (the live-verified path). job: jobs.Job for progress."""
    from app.willhaben import insert

    if draft_id in _publishing:
        raise insert.InsertError("Wird gerade veröffentlicht – bitte warten")
    _publishing.add(draft_id)
    try:
        return await _publish_willhaben(draft_id, option, values, stop_after, shipping, job)
    finally:
        _publishing.discard(draft_id)


def enqueue_publish(draft_id: int, option: int, values: dict[str, str], shipping: dict[str, Any] | None = None) -> bool:
    """Check the form right away (errors land on the draft at once), then publish as a background job.

    Returns False when nothing was queued (input error, already queued or published); the reason is in d.error.
    """
    from app.services import jobs
    from app.willhaben import insert

    with session() as db:
        d = db.get(AdDraft, draft_id)
    if d.publishing:
        return False
    if d.refining:                        # publishing now would upload the text the revision is about to replace
        update_draft(draft_id, error=t(REFINING_BUSY))
        return False
    try:
        _publish_inputs(d, option, values, shipping)
    except insert.InsertError as e:
        update_draft(draft_id, error=t("Publishing failed: {error}", error=str(e)))
        return False
    d = update_draft(draft_id, publishing=True, error=None)
    n_photos = len(d.image_paths[:insert.MAX_PHOTOS])

    async def run(job) -> str:
        try:
            await publish_willhaben(draft_id, option, values, shipping=shipping, job=job)
        except Exception as e:
            log.warning("publishing draft %s failed: %s", draft_id, e)
            update_draft(draft_id, error=t("Publishing failed: {error}", error=str(e)))
            raise
        finally:
            update_draft(draft_id, publishing=None)
        return t("Published on willhaben")

    jobs.submit("publish", t("Publish „{title}“", title=(d.title or f"#{draft_id}")[:60]), run,
                total=n_photos + 5, current=f"addraft:{draft_id}")
    return True


def enqueue_refine(draft_id: int, instruction: str) -> bool:
    """"Revise with Claude" as a background job (lane claude, 10–60 s): the draft shows "Claude is revising…" and
    swaps itself in when done. False: nothing to do or already on its way."""
    from app.services import jobs

    with session() as db:
        d = db.get(AdDraft, draft_id)
    if d is None or d.refining or d.publishing or d.status == "published" or not instruction.strip():
        return False                      # never rewrite a draft that is on its way to willhaben or already there
    update_draft(draft_id, refining=True, error=None)

    async def run(job) -> str:
        try:
            d = await refine_draft(draft_id, instruction)
        finally:
            update_draft(draft_id, refining=None)
        if d.error:                       # refine_draft keeps Claude's errors on the draft
            raise RuntimeError(d.error)
        return t("Revised: {title}", title=(d.title or "")[:60])

    jobs.submit("refine", t("Claude revises „{title}“", title=(d.title or f"#{draft_id}")[:60]), run, lane="claude",
                current=f"refining:{draft_id}")
    return True


def recover_publishing() -> None:
    """After a restart the queue is gone. The willhaben draft id is remembered, so publishing again continues it."""
    with session() as db:
        for d in db.exec(select(AdDraft).where(AdDraft.publishing == True)):  # noqa: E712
            d.publishing, d.error = None, t("Publishing interrupted by a restart – check on willhaben, then try again")
            db.add(d)
        for d in db.exec(select(AdDraft).where(AdDraft.refining == True)):  # noqa: E712
            d.refining = None             # the revision never happened: the draft is as it was
            db.add(d)
        db.commit()


def _publish_inputs(d: AdDraft, option: int, values: dict[str, str], shipping: dict[str, Any] | None):
    """Everything that can be checked without willhaben: (chosen option, attrs, labels, shipping) or InsertError."""
    from app.willhaben import insert

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
        update_draft(d.id, wh_plan={**plan, "shipping": shipping, "selected": option})

    if d.wh_ad_id and plan.get("delivery_set") and shipping["handover"] == "pickup":
        # A former attempt already set shipping/PayLivery options on this willhaben draft; saving it with pickup
        # would not clear them (a reset call is unverified), so the user changes the handover on willhaben.
        raise insert.InsertError("Versand wurde bei willhaben schon eingerichtet – Übergabe bitte direkt auf "
                                 "willhaben ändern oder wieder Versand wählen")
    return chosen, attrs, labels, shipping


async def _publish_willhaben(draft_id: int, option: int, values: dict[str, str], stop_after: str | None,
                             shipping: dict[str, Any] | None, job: Any = None):
    from app.willhaben import insert
    from app.willhaben.session import get_session

    with session() as db:
        d = db.get(AdDraft, draft_id)
    chosen, attrs, labels, shipping = _publish_inputs(d, option, values, shipping)
    with session() as db:
        d = db.get(AdDraft, draft_id)             # wh_plan may just have been updated
    done = [0]

    def on_step(step: str, i: int = 0, n: int = 0) -> None:
        if job is None:
            return
        done[0] += 1
        text = {"form": t("Loading the willhaben form"), "presave": t("Creating the willhaben draft"),
                "photo": t("Uploading photo {i}/{n}", i=i, n=n), "save": t("Saving the ad"),
                "delivery": t("Setting up shipping"), "activate": t("Activating the free ad")}[step]
        job.progress(min(done[0], job.total), text, f"addraft:{draft_id}")

    on_step("form")
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

    def delivery_set() -> None:                  # shipping options now live on the willhaben draft (see guard above)
        with session() as db:
            cur = dict(db.get(AdDraft, draft_id).wh_plan or {})
        update_draft(draft_id, wh_plan={**cur, "delivery_set": True})

    ship = shipping if shipping["handover"] != "pickup" else None
    try:
        res = await insert.publish(sess, ad, photos, existing_ad_id=d.wh_ad_id, on_presaved=remember,
                                   stop_after=stop_after, shipping=ship, on_delivery=delivery_set, on_step=on_step)
    except RuntimeError as e:
        # 409 on the reused willhaben draft: it was deleted (or finished) meanwhile -> start one fresh insertion
        if not d.wh_ad_id or "HTTP 409" not in str(e):
            raise
        # ...unless it still exists: then it may already be live, and a new insertion would be a public duplicate
        from app.willhaben.account import AccountClient
        old = next((a for a in await AccountClient(sess).my_adverts() if a.ad_id == str(d.wh_ad_id)), None)
        if old is not None:
            if old.status == "active":
                update_draft(draft_id, status="published", error=None)
            raise insert.InsertError(f"Existiert auf willhaben schon als {old.ad_id} "
                                     f"({old.status_label or old.status}) – nicht noch einmal angelegt") from e
        log.info("willhaben draft %s of ad draft %s is gone (%s); publishing as a new insertion", d.wh_ad_id, draft_id, e)
        update_draft(draft_id, wh_ad_id=None)
        res = await insert.publish(sess, ad, photos, existing_ad_id=None, on_presaved=remember, stop_after=stop_after,
                                   shipping=ship, on_delivery=delivery_set, on_step=on_step)
    if res.get("stopped"):                       # step-by-step trial: nothing public yet, draft stays a draft
        return res
    d = update_draft(draft_id, status="published", wh_ad_id=res["ad_id"], error=None)
    from app.db import MyAd
    from app.services import my_ads
    parcel_estimate = clean_parcel(d.parcel)       # source "claude": the shipping card says "estimated"
    with session() as db:                        # show up under "my ads" right away, status unknown yet
        row = db.get(MyAd, res["ad_id"])
        if row is None:
            row = MyAd(ad_id=res["ad_id"], heading=d.title, url=res["url"], status="unknown", image_url=None,
                       published_at=utcnow(), parcel=parcel_estimate)
        elif row.parcel is None:                 # the account import was faster than we were: fill the gaps only
            row.parcel = parcel_estimate
        if hasattr(row, "quantity") and row.quantity is None:     # MyAd.quantity arrives with the inventory change
            row.quantity = d.quantity
        db.add(row)
        db.commit()
    try:                                         # willhaben's own status (new ads are reviewed before going online)
        await my_ads.import_from_account()
    except Exception as e:  # noqa: BLE001 - the ad is published either way
        log.info("my-ads refresh after publishing failed: %s", e)
    log.info("draft %s published on willhaben as %s", draft_id, res["ad_id"])
    return d
