"""Own ads: imported from the account, expiry tracked via the public detail page, warnings, reactivation."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from sqlmodel import select

from app.db import MyAd, get_setting, session, utcnow
from app.i18n import t
from app.services.notify import notify
from app.willhaben.account import ACTION_DELETE, AccountClient, EndpointUnknown
from app.willhaben.parse import extract_ad_id
from app.willhaben.public import Blocked, WillhabenError, get_public
from app.willhaben.session import NotLoggedIn, SessionUnavailable, get_session

log = logging.getLogger(__name__)

MYADVERTS_URL = "https://www.willhaben.at/iad/myprofile/myadverts"


def is_sold(ad: MyAd) -> bool:
    """Sold on willhaben, given away ("Verschenkt") or marked sold in the app."""
    return ad.status in ("sold", "Verschenkt") or ad.sale_status == "sold"


def add(ref: str, auto_renew: bool = False) -> MyAd:
    ad_id = extract_ad_id(ref)
    if not ad_id:
        raise ValueError(t("Expected an ad ID or willhaben link"))
    with session() as db:
        row = db.get(MyAd, ad_id) or MyAd(ad_id=ad_id)
        row.auto_renew = auto_renew or row.auto_renew
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def remove(ad_id: str) -> None:
    with session() as db:
        row = db.get(MyAd, ad_id)
        if row:
            db.delete(row)
            db.commit()


def _unlink_drafts(ad_id: str) -> None:
    """The willhaben ad is gone: drafts that pointed to it start a fresh insertion when published again."""
    from app.db import AdDraft
    with session() as db:
        for d in db.exec(select(AdDraft).where(AdDraft.wh_ad_id == str(ad_id))):
            d.wh_ad_id = None
            if d.status == "published":
                d.status = "draft"
            db.add(d)
        db.commit()


def set_hidden(ad_id: str, hidden: bool) -> None:
    """Hide from the list only (nothing happens on willhaben); the daily import keeps it hidden."""
    with session() as db:
        row = db.get(MyAd, ad_id)
        if row:
            row.hidden = hidden or None
            db.add(row)
            db.commit()


async def deactivate(ad_id: str) -> MyAd | None:
    """Take the ad offline on willhaben (reversible). Auto-renew goes off: offline was intended."""
    await AccountClient(get_session()).deactivate_ad(ad_id)
    with session() as db:
        row = db.get(MyAd, ad_id)
        if row:
            row.auto_renew, row.last_action = False, t("deactivated on willhaben {when}", when=f"{utcnow():%d.%m. %H:%M}")
            db.add(row)
            db.commit()
    await import_from_account()            # status as willhaben shows it now
    with session() as db:
        return db.get(MyAd, ad_id)


async def delete(ad_id: str) -> None:
    """Delete the ad on willhaben for good, confirm it is gone, then drop it here (so auto-renew never sees it)."""
    acct = AccountClient(get_session())
    await acct.delete_ad(ad_id)
    if any(a.ad_id == str(ad_id) for a in await acct.my_adverts()):
        raise EndpointUnknown(t("willhaben still lists the ad after deleting – please check on willhaben"))
    remove(ad_id)
    _unlink_drafts(ad_id)
    log.info("ad %s deleted on willhaben", ad_id)


async def bulk(action: str, ad_ids: list[str]) -> dict[str, Any]:
    """Activate, deactivate or delete several selected ads, one by one with willhaben's per-ad links.

    One list request before and one after; every delete is confirmed against the account list before the
    row goes. Activating a passive or expired ad republishes it under a new id (like auto-renew).
    Returns {"done": [titles], "republished": [titles], "failed": [(title, error)]}.
    """
    if action not in ("activate", "deactivate", "delete"):
        raise ValueError(action)
    acct = AccountClient(get_session())
    ads = await acct.my_adverts()
    titles = {a.ad_id: a.heading or a.ad_id for a in ads}
    done, failed, moved = [], [], {}
    queue = list(dict.fromkeys(ad_ids))
    with session() as db:
        sold_here = {x for x in queue if (r := db.get(MyAd, x)) and is_sold(r)}
    for i, ad_id in enumerate(queue):   # strictly one after another, each request through the shared limiter
        title = titles.get(ad_id, ad_id)
        try:
            if action == "delete":
                await acct.delete_ad(ad_id, ads)
            elif action == "activate":
                if ad_id in sold_here:    # putting a sold ad online again would sell it twice
                    failed.append((title, t("sold – not put online again")))
                    continue
                if (new_id := await acct.reactivate(ad_id, ads)) != ad_id:
                    moved[ad_id] = new_id
                    _move_to_new_id(ad_id, new_id)    # at once: a later error must not leave it re-activatable
            else:
                await acct.deactivate_ad(ad_id, ads)
            done.append(ad_id)
        except (NotLoggedIn, SessionUnavailable, Blocked) as e:    # logged out / rate limited: stop right here
            failed.append((title, str(e)))
            failed += [(titles.get(x, x), t("not done (stopped after an error)")) for x in queue[i + 1:]]
            break
        except (EndpointUnknown, RuntimeError) as e:    # InsertError of a republish is a RuntimeError
            failed.append((title, str(e)))
    if action == "delete":
        still = {a.ad_id for a in await acct.my_adverts()}
        for ad_id in list(done):
            if ad_id in still:
                done.remove(ad_id)
                failed.append((titles.get(ad_id, ad_id), t("willhaben still lists the ad after deleting – please check on willhaben")))
            else:
                remove(ad_id)
                _unlink_drafts(ad_id)
    else:
        with session() as db:
            for ad_id in done:
                row = db.get(MyAd, ad_id)
                if row:
                    when = f"{utcnow():%d.%m. %H:%M}"
                    if action == "deactivate":
                        row.auto_renew = False    # offline on purpose: auto-renew must not undo it
                        row.last_action = t("deactivated on willhaben {when}", when=when)
                    else:
                        row.last_action = t("activated on willhaben {when}", when=when)
                    db.add(row)
            db.commit()
        await import_from_account()
    log.info("bulk %s: %d done (%d republished), %d failed", action, len(done), len(moved), len(failed))
    return {"done": [titles.get(i, i) for i in done], "republished": [titles.get(i, i) for i in moved],
            "failed": failed}


async def cleanup_candidates() -> list[dict[str, Any]]:
    """Old ads worth deleting on willhaben: offline without photos (cannot be put online again), sold or given
    away, and the old copy of a republished ad. Never an active or reserved one.

    Read from the live account list: the app keeps the last known photo, willhaben drops them from old ads.
    """
    ads = await AccountClient(get_session()).my_adverts()
    with session() as db:
        rows = {r.ad_id: r for r in db.exec(select(MyAd))}
    out = []
    for a in ads:
        if a.status in ("active", "reserved"):
            continue
        row = rows.get(a.ad_id)
        reasons = []
        if row and row.replaced_by:
            reasons.append(t("republished as {id}", id=row.replaced_by))
        if a.status in ("sold", "given_away") or (row and is_sold(row)):
            reasons.append(t("sold or given away"))
        if not a.image_url:
            reasons.append(t("no photo"))
        if reasons:
            out.append({"ad_id": a.ad_id, "heading": a.heading or a.ad_id, "url": a.url,
                        "status": a.status_label or a.status, "reasons": reasons,
                        "deletable": ACTION_DELETE in (a.actions or {})})
    return out


async def cleanup(ad_ids: list[str]) -> dict[str, Any]:
    """Delete the picked candidates on willhaben; ones willhaben offers no deleting for are only hidden here.

    The candidates are fetched again, so nothing outside them (an active ad, say) is ever deleted.
    Returns bulk()'s result plus {"hidden": [titles]}.
    """
    cands = {c["ad_id"]: c for c in await cleanup_candidates()}
    picked = [i for i in dict.fromkeys(ad_ids) if i in cands]
    hide = [i for i in picked if not cands[i]["deletable"]]
    for ad_id in hide:
        set_hidden(ad_id, True)
    delete = [i for i in picked if cands[i]["deletable"]]
    res = await bulk("delete", delete) if delete else {"done": [], "failed": []}
    res["hidden"] = [cands[i]["heading"] for i in hide]
    return res


async def load_for_edit(ad_id: str) -> dict[str, Any]:
    """Current title, description (plain text) and price as willhaben has them."""
    from app.willhaben import insert
    s = get_session()
    ad = await insert.get_own_ad(s, ad_id)
    try:
        images = [{"reference": i["reference"], "thumb": insert.image_thumb(i)}
                  for i in (await insert.get_own_images(s, ad))["advertImage"]]
        images_error = None
    except (insert.InsertError, RuntimeError, ValueError) as e:      # text editing still works without photos
        log.info("photos of ad %s not loadable: %s", ad_id, e)
        images, images_error = [], str(e)
    return {"heading": ad.get("heading") or "", "description": insert.description_text(ad.get("description")),
            "price": ad.get("price"), "images": images, "images_error": images_error}


PHOTO_MIMES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
               ".gif": "image/gif"}


async def set_photos(ad_id: str, order: list[str], new_photos: list[tuple[str, bytes]]) -> MyAd | None:
    """Reorder / remove / add photos on willhaben (first = cover), then refresh the status."""
    from pathlib import Path

    from app.willhaben import insert
    files = []
    for name, data in new_photos:
        mime = PHOTO_MIMES.get(Path(name).suffix.lower())
        if mime is None:
            raise insert.InsertError(t("Unsupported photo format: {name} (JPG, PNG, WebP)", name=name))
        files.append((name, data, mime))
    await insert.set_own_images(get_session(), ad_id, order, files)
    with session() as db:
        row = db.get(MyAd, ad_id)
        if row:
            row.last_action = t("photos changed on willhaben {when}", when=f"{utcnow():%d.%m. %H:%M}")
            db.add(row)
            db.commit()
    try:
        await import_from_account()
    except (NotLoggedIn, SessionUnavailable, ValueError) as e:
        log.info("status refresh after photo change skipped: %s", e)
    with session() as db:
        return db.get(MyAd, ad_id)


async def ai_revise(ad_id: str, heading: str, description: str, price: float | None,
                    instruction: str) -> dict[str, Any]:
    """Claude revises title and description of a live ad by one instruction (nothing is saved).

    Gets the current text from the edit form, the ad's photos (up to 4) and category from the public page.
    Returns {heading, description, price_estimate, note} as a proposal for the form.
    """
    import tempfile
    from pathlib import Path

    import httpx

    from app.llm.base import get_backend
    from app.llm.prompts import AD_SCHEMA, AD_SYSTEM, ad_refine_prompt

    instruction = instruction.strip() or "Überarbeite das Inserat: klarer, verkaufsstärker, ehrlich."
    detail = None
    try:
        detail = await get_public().get_detail(ad_id)
    except WillhabenError as e:
        log.info("ad %s detail for AI edit failed: %s", ad_id, e)
    current = {"title": heading, "description": description,
               "category_hint": " > ".join(getattr(detail, "category_path", None) or []),
               "condition": "", "attributes": []}
    with tempfile.TemporaryDirectory(prefix="wh-edit-") as tmp:
        photos: list[Path] = []
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
            for i, url in enumerate((getattr(detail, "images", None) or [])[:4], 1):
                try:
                    r = await c.get(url)
                except httpx.HTTPError:
                    continue
                if r.status_code == 200 and r.content and len(r.content) < 3 * 1024 * 1024:
                    p = Path(tmp) / f"foto{i}.jpg"
                    p.write_bytes(r.content)
                    photos.append(p)
        prompt = ad_refine_prompt(notes=f"Aktueller Preis: {price:g} €" if price else "", rules=get_setting("reply_rules"),
                                  n_images=len(photos), current=current, specs=None, product_name=None,
                                  instruction=instruction)
        result = await get_backend("ads").complete(system=AD_SYSTEM, prompt=prompt, schema=AD_SCHEMA, images=photos)
    d = result.data
    return {"heading": (d.get("title") or heading).strip()[:80], "description": (d.get("description") or description).strip(),
            "price_estimate": d.get("price_estimate"), "note": (d.get("note") or "").strip()}


async def edit(ad_id: str, heading: str, description: str, price: float | None) -> MyAd | None:
    """Save title/description/price on willhaben, then refresh the status (an edit may go to review)."""
    from app.willhaben import insert
    if not heading.strip() or not description.strip():
        raise insert.InsertError(t("Title and description must not be empty"))
    await insert.edit_own_ad(get_session(), ad_id, heading=heading, description=description, price=price)
    with session() as db:
        row = db.get(MyAd, ad_id)
        if row:
            row.heading = heading.strip()[:80]
            row.last_action = t("edited on willhaben {when}", when=f"{utcnow():%d.%m. %H:%M}")
            db.add(row)
            db.commit()
    try:
        await import_from_account()
    except (NotLoggedIn, SessionUnavailable, ValueError) as e:
        log.info("status refresh after edit skipped: %s", e)
    with session() as db:
        return db.get(MyAd, ad_id)


def set_auto_renew(ad_id: str, on: bool) -> None:
    with session() as db:
        row = db.get(MyAd, ad_id)
        if row:
            row.auto_renew = on
            db.add(row)
            db.commit()


_KEEP: Any = object()


def set_auto_reply(ad_id: str, on: bool, instructions: str, min_price: float | None = _KEEP) -> None:
    """Overwrites the instructions; `min_price` only changes when passed (None clears the floor)."""
    with session() as db:
        row = db.get(MyAd, ad_id)
        if row:
            row.auto_reply, row.auto_reply_instructions = on, instructions.replace("\r\n", "\n").strip() or None
            if min_price is not _KEEP:
                row.min_price = min_price
            db.add(row)
            db.commit()


async def import_from_account() -> set[str]:
    """Add/update every ad of the logged-in account; returns their ids.

    The account list is the better source: it also knows deactivated and sold ads, which the public
    detail page would only report as gone.
    """
    ads = await AccountClient(get_session()).my_adverts()
    newly_expired = []
    with session() as db:
        for ad in ads:
            row = db.get(MyAd, ad.ad_id) or MyAd(ad_id=ad.ad_id)
            previous = row.status
            row.heading = ad.heading or row.heading
            row.url, row.image_url = ad.url, ad.image_url or row.image_url
            row.status = ad.status if ad.status in ("active", "expired", "sold", "reserved") else ad.status_label or ad.status
            row.published_at, row.end_date = ad.start or row.published_at, ad.end or row.end_date
            row.last_checked_at, row.last_error = utcnow(), None
            db.add(row)
            if previous == "active" and row.status == "expired":
                newly_expired.append(row)
        db.commit()
        for row in newly_expired:
            db.refresh(row)
    for row in newly_expired:
        await notify("ad_expired", t("Ad expired"), row.heading or row.ad_id, url=MYADVERTS_URL,
                     image=row.image_url, data={"ad_id": row.ad_id})
    return {ad.ad_id for ad in ads}


async def check(ad_id: str) -> MyAd | None:
    try:
        detail = await get_public().get_detail(ad_id)
    except WillhabenError as e:
        with session() as db:
            row = db.get(MyAd, ad_id)
            if row:
                row.last_error, row.last_checked_at = str(e), utcnow()
                db.add(row)
                db.commit()
        return None
    with session() as db:
        row = db.get(MyAd, ad_id)
        if row is None:
            return None
        previous = row.status
        if previous == "sold":
            pass            # sold on willhaben: the public page only says "gone", keep the better status
        elif detail is None:
            row.status = "expired"
        else:
            row.status = "active" if detail.status == "active" else "expired"
            row.heading, row.url = detail.heading, detail.url
            row.image_url = detail.images[0] if detail.images else row.image_url
            row.published_at, row.end_date = detail.published_at, detail.end_date
        row.last_checked_at, row.last_error = utcnow(), None
        db.add(row)
        db.commit()
        db.refresh(row)
    if previous == "active" and row.status == "expired":
        await notify("ad_expired", t("Ad expired"), row.heading or row.ad_id, url=MYADVERTS_URL,
                     image=row.image_url, data={"ad_id": row.ad_id})
    return row


async def _reactivate(row: MyAd) -> None:
    from app.willhaben.insert import InsertError

    sess = get_session()
    new_id = row.ad_id
    try:
        new_id = await AccountClient(sess).reactivate(row.ad_id)
        action, err = t("reactivated"), None
    except (EndpointUnknown, NotLoggedIn, SessionUnavailable, InsertError, RuntimeError) as e:
        action, err = t("Reactivation needed manually"), str(e)
    with session() as db:
        r = db.get(MyAd, row.ad_id)
        r.last_action, r.last_error = action, err
        db.add(r)
        db.commit()
    if not err and new_id != row.ad_id:
        row = _move_to_new_id(row.ad_id, new_id)
    if err:
        await notify("ad_expired", t("Please reactivate the ad"), f"{row.heading or row.ad_id} ({action})",
                     url=MYADVERTS_URL, image=row.image_url, data={"ad_id": row.ad_id})
    else:
        await notify("ad_renewed", t("Ad renewed automatically"), row.heading or row.ad_id, url=row.url,
                     image=row.image_url, data={"ad_id": row.ad_id})
        await check(row.ad_id)


def _move_to_new_id(old_id: str, new_id: str) -> MyAd:
    """A republished ad has a new willhaben id: copy the per-ad settings over. The old row stays (marked
    replaced, auto-renew off) because buyer chats keep pointing at the old id and still need its auto-reply
    settings."""
    keep = ("heading", "image_url", "auto_renew", "auto_reply", "auto_reply_instructions", "published_at")
    with session() as db:
        old = db.get(MyAd, old_id)
        new = db.get(MyAd, new_id) or MyAd(ad_id=new_id)
        for k in keep:
            if hasattr(old, k):
                setattr(new, k, getattr(old, k))
        new.url, new.status = f"https://www.willhaben.at/iad/object?adId={new_id}", "active"
        new.last_action = t("republished (was {id})", id=old_id)
        old.replaced_by, old.auto_renew, old.status = new_id, False, "replaced"
        db.add(new)
        db.add(old)
        db.commit()
        db.refresh(new)
    log.info("ad %s republished as %s", old_id, new_id)
    return new


async def check_all() -> dict[str, int]:
    warn_days = int(get_setting("expiry_warn_days") or 3)
    imported: set[str] = set()
    try:
        imported = await import_from_account()
    except (NotLoggedIn, SessionUnavailable, ValueError) as e:
        log.info("my-ads import skipped: %s", e)
    with session() as db:
        ids = [r.ad_id for r in db.exec(select(MyAd))]
    counts = {"imported": len(imported), "checked": 0, "warned": 0, "reactivation": 0}
    for ad_id in ids:
        if ad_id in imported:
            with session() as db:
                row = db.get(MyAd, ad_id)
        else:
            row = await check(ad_id)   # added by hand / not in the account list: public detail page
        if row is None:
            continue
        counts["checked"] += 1
        if row.sale_status == "sold":
            continue    # sold (sold workflow): no expiry warnings, never reactivated/republished
        if row.status == "active" and row.end_date and row.end_date - utcnow() < timedelta(days=warn_days) \
                and row.notified_expiry_for != row.end_date:
            await notify("ad_expiring", t("Ad expires soon"),
                         t("{title}: ends {when} UTC", title=row.heading, when=f"{row.end_date:%d.%m. %H:%M}"), url=row.url,
                         image=row.image_url, data={"ad_id": row.ad_id, "end_date": row.end_date.isoformat()})
            with session() as db:
                r = db.get(MyAd, ad_id)
                r.notified_expiry_for = row.end_date
                db.add(r)
                db.commit()
            counts["warned"] += 1
        if row.status == "expired" and row.auto_renew:
            await _reactivate(row)
            counts["reactivation"] += 1
    return counts
