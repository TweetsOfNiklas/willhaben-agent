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
from app.willhaben.public import WillhabenError, get_public
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
    sess = get_session()
    try:
        await AccountClient(sess).reactivate(row.ad_id)
        action, err = t("reactivated"), None
    except (EndpointUnknown, NotLoggedIn, SessionUnavailable) as e:
        action, err = t("Reactivation needed manually"), str(e)
    with session() as db:
        r = db.get(MyAd, row.ad_id)
        r.last_action, r.last_error = action, err
        db.add(r)
        db.commit()
    if err:
        await notify("ad_expired", t("Please reactivate the ad"), f"{row.heading or row.ad_id} ({action})",
                     url=MYADVERTS_URL, image=row.image_url, data={"ad_id": row.ad_id})
    else:
        await check(row.ad_id)


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
