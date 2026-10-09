"""Own ads: imported from the account, expiry tracked via the public detail page, warnings, reactivation."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from sqlmodel import select

from app.db import MyAd, get_setting, session, utcnow
from app.i18n import t
from app.services.notify import notify
from app.willhaben.account import AccountClient, EndpointUnknown
from app.willhaben.parse import extract_ad_id
from app.willhaben.public import Blocked, WillhabenError, get_public
from app.willhaben.session import NotLoggedIn, SessionUnavailable, get_session

log = logging.getLogger(__name__)

MYADVERTS_URL = "https://www.willhaben.at/iad/myprofile/myadverts"


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
    """Deactivate or delete several selected ads, one by one with willhaben's per-ad links.

    One list request before and one after; every delete is confirmed against the account list before the
    row goes. Returns {"done": [titles], "failed": [(title, error)]}.
    """
    if action not in ("activate", "deactivate", "delete"):
        raise ValueError(action)
    acct = AccountClient(get_session())
    ads = await acct.my_adverts()
    titles = {a.ad_id: a.heading or a.ad_id for a in ads}
    done, failed = [], []
    queue = list(dict.fromkeys(ad_ids))
    for i, ad_id in enumerate(queue):   # strictly one after another, each request through the shared limiter
        title = titles.get(ad_id, ad_id)
        try:
            if action == "delete":
                await acct.delete_ad(ad_id, ads)
            elif action == "activate":
                await acct.activate_ad(ad_id, ads)
            else:
                await acct.deactivate_ad(ad_id, ads)
            done.append(ad_id)
        except (NotLoggedIn, SessionUnavailable, Blocked) as e:    # logged out / rate limited: stop right here
            failed.append((title, str(e)))
            failed += [(titles.get(x, x), t("not done (stopped after an error)")) for x in queue[i + 1:]]
            break
        except (EndpointUnknown, RuntimeError) as e:
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
    log.info("bulk %s: %d done, %d failed", action, len(done), len(failed))
    return {"done": [titles.get(i, i) for i in done], "failed": failed}


def set_auto_renew(ad_id: str, on: bool) -> None:
    with session() as db:
        row = db.get(MyAd, ad_id)
        if row:
            row.auto_renew = on
            db.add(row)
            db.commit()


def set_auto_reply(ad_id: str, on: bool, instructions: str) -> None:
    with session() as db:
        row = db.get(MyAd, ad_id)
        if row:
            row.auto_reply, row.auto_reply_instructions = on, instructions.replace("\r\n", "\n").strip() or None
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
            row.status = ad.status if ad.status in ("active", "expired") else ad.status_label or ad.status
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
        if detail is None:
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
