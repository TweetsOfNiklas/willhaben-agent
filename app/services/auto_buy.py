"""Auto-buy (a search agent's switch "Buy automatically"): a hit that passes the agent's rules (the ones that push it)
and costs at most the agent's auto-buy maximum – item, shipping and the buyer protection fee – is bought on its own.

willhaben: with PayLivery's "Sofort kaufen" only (an ad without it is left alone). The purchase is a purchase order in
PayLivery mode; it waits COUNTDOWN_S in the queue first – Telegram says so with a Cancel button, the jobs list and the
order page can take it back too – then reads the ad again (still online, still buy-now, the total still within the
maximum) and runs the PayLivery card's buy step in the app's browser, up to the payment with the saved card; 3-D Secure
is approved in the bank app. The order has no chat yet: it gets a placeholder conversation (BUY_CHAT_PREFIX, hidden)
for the buy step, and the real willhaben chat takes the order over once it syncs (purchase.on_paylivery_stage).

Vinted: not yet (its checkout has to be recorded first); such hits are left alone.
"""
from __future__ import annotations

import logging
from sqlmodel import col, select

from app.db import Conversation, Hit, Purchase, SearchAgent, session, utcnow
from app.i18n import t

log = logging.getLogger(__name__)

COUNTDOWN_S = 120
BUY_CHAT_PREFIX = "autobuy-"
PLATFORMS = ("willhaben",)


def enabled(agent: SearchAgent) -> bool:
    return bool(agent.auto_buy and agent.auto_buy_max and agent.auto_buy_max > 0)


def estimate(h: Hit) -> float | None:
    """What the hit would cost with PayLivery (item, its shipping or the estimate, the buyer protection fee) – the same
    sums as a purchase order's (purchase.costs_of); None without a price."""
    from app.services import purchase
    if not h.price or h.price <= 0:
        return None
    row = Purchase(ad_id=h.ad_id, mode="paylivery", shipping_cost=h.shipping_price,
                   shipping_source="ad" if h.shipping_price is not None else None)
    return purchase.costs_of(row, float(h.price))["total"]


def why_not(agent: SearchAgent, h: Hit) -> str | None:
    """Why this hit would not be bought (no request; the rules that push it are the caller's), None = it would."""
    if not enabled(agent):
        return t("auto-buy is off")
    if (h.platform or "willhaben") not in PLATFORMS:
        return t("only willhaben so far")
    total = estimate(h)
    if total is None:
        return t("no price")
    if total > agent.auto_buy_max + 0.005:
        return t("{total} is above the maximum {max}", total=_eur(total), max=_eur(agent.auto_buy_max))
    if h.screen_relevant is False:
        return t("not the item")
    issues = set(h.screen_issues or [])
    if "scam_suspect" in issues:
        return t("suspected scam")
    if "defective" in issues:
        return t("defective")
    if h.site_status in ("sold", "reserved", "gone", "expired"):
        return t("no longer for sale")
    return None


def candidate(agent: SearchAgent, h: Hit) -> bool:
    """Worth a look (no request): the agent buys, a site it can buy on, the estimated total within the maximum, not a
    scam suspect, not defective, not judged the wrong item. Buy-now is only known from the ad page (buy())."""
    return why_not(agent, h) is None


async def consider(agent: SearchAgent, hits: list[Hit]) -> list[int]:
    """The passing hits of a check: each candidate becomes an auto-buy order with its countdown. Returns their ids."""
    started = []
    for h in hits:
        if not candidate(agent, h):
            continue
        try:
            pid = await schedule(agent, h)
        except Exception as e:  # noqa: BLE001 - one ad that cannot be bought must not stop the check
            log.warning("auto-buy of %s not started: %s", h.ad_id, e)
            continue
        if pid:
            started.append(pid)
    return started


async def schedule(agent: SearchAgent, h: Hit) -> int | None:
    """Store the order (status "new", the agent's maximum) and queue the buy behind the countdown; tell Telegram."""
    from app.services import jobs, purchase
    from app.services.notify import notify

    with session() as db:
        if db.exec(select(Purchase).where(Purchase.ad_id == h.ad_id, col(Purchase.status).in_(purchase.OPEN))).first():
            return None                   # an order for this ad runs already (a price drop of the same ad)
        row = Purchase(ad_id=h.ad_id, ad_title=h.heading, ad_url=h.url, image_url=h.image_url, asking_price=h.price,
                       max_price=agent.auto_buy_max, mode="paylivery", status="new", log=[],
                       shipping_cost=h.shipping_price, shipping_source="ad" if h.shipping_price is not None else None)
        purchase._line(row, t("Auto-buy by the search agent „{name}“ – buying in {n} s unless cancelled",
                              name=agent.name, n=COUNTDOWN_S))
        db.add(row)
        db.commit()
        db.refresh(row)
        pid = row.id

    async def run(job) -> str:
        return await buy(pid)

    jobs.submit("auto-buy", t("Auto-buy „{title}“", title=(h.heading or h.ad_id)[:60]), run, lane="chat",
                current=f"purchase:{pid}", on_cancel=lambda: cancelled(pid), grace=COUNTDOWN_S, link=f"/purchases/{pid}")
    total = _total(pid, h.price)
    await notify("purchase", t("Buying in {n} min: {title}", n=COUNTDOWN_S // 60, title=h.heading or h.ad_id),
                 t("{price} incl. shipping and buyer protection · your maximum {max}", price=_eur(total),
                   max=_eur(agent.auto_buy_max)),
                 url=h.url, image=h.image_url, tag=f"wh-autobuy-{pid}",
                 data={"purchase_id": pid, "auto_buy": True, "ad_id": h.ad_id, "price": h.price})
    return pid


def cancelled(pid: int) -> None:
    """Taken back in the countdown (Telegram's Cancel, the jobs list): nothing was bought."""
    from app.services import purchase
    row = purchase.get(pid)
    if row is not None and row.status == "new":
        purchase._change(pid, lambda r: purchase._move(r, "cancelled", t("Auto-buy cancelled before buying")))


async def buy(pid: int) -> str:
    """After the countdown: read the ad again, check it, buy it with PayLivery (the buy step in the browser)."""
    from app import platforms
    from app.services import paylivery, purchase
    from app.services.notify import notify

    row = purchase.get(pid)
    if row is None or row.status != "new":
        return t("Cancelled")
    try:
        d = await platforms.get("willhaben").detail(row.ad_id, url=row.ad_url)
    except Exception as e:  # noqa: BLE001 - the ad cannot be read: nothing is bought
        return await _stop(pid, t("The ad could not be read: {error}", error=e))
    if (d.status or "active") != "active":
        return await _stop(pid, t("The ad is no longer for sale"))
    if not (d.paylivery_enabled and d.buy_now):
        return await _stop(pid, t("The ad has no PayLivery buy-now price"))
    if not d.price:
        return await _stop(pid, t("The ad shows no price"))
    purchase._change(pid, lambda r: (setattr(r, "asking_price", d.price),
                                     setattr(r, "shipping_cost", d.shipping_price if d.shipping_price is not None else r.shipping_cost),
                                     setattr(r, "shipping_source", "ad" if d.shipping_price is not None else r.shipping_source)))
    total = _total(pid, d.price)
    row = purchase.get(pid)
    if row.max_price and total > row.max_price + 0.005:
        return await _stop(pid, t("{total} incl. shipping and buyer protection is above the maximum {max}",
                                  total=_eur(total), max=_eur(row.max_price)))
    cid = BUY_CHAT_PREFIX + str(row.ad_id)
    with session() as db:              # the buy step works on a chat: a hidden placeholder until willhaben's arrives
        conv = db.get(Conversation, cid) or Conversation(id=cid)
        conv.platform, conv.role, conv.ad_id, conv.ad_title = "willhaben", "buyer", row.ad_id, d.heading
        conv.partner_name, conv.ad_context = d.seller_name, d.as_context()
        conv.hidden_at, conv.removed, conv.last_message_at = utcnow(), True, utcnow()
        db.add(conv)
        db.commit()
    purchase._change(pid, lambda r: (setattr(r, "conversation_id", cid), setattr(r, "agreed_price", d.price),
                                     setattr(r, "agreed_total", total),
                                     purchase._move(r, "pay_now", t("Buying automatically with PayLivery"))))
    try:
        result = await paylivery.run_action(cid, "buy", float(d.price))
    except paylivery.PayLiveryError as e:
        return await _stop(pid, str(e))
    row = purchase.get(pid)
    if row is not None and row.status == "paid":
        await notify("purchase", t("Bought: {title}", title=row.ad_title or row.ad_id),
                     t("{price} paid with PayLivery", price=_eur(total)), url=row.ad_url, tag=f"wh-autobuy-{pid}",
                     data={"purchase_id": pid})
        return t("Bought")
    await purchase.pause(pid, t("Auto-buy did not finish: {result}", result=result[:300]), push=True)
    return t("Not bought")


async def _stop(pid: int, reason: str) -> str:
    """Nothing bought: the order fails with the reason, Telegram hears it."""
    from app.services import purchase
    await purchase._fail(pid, t("Auto-buy stopped: {reason}", reason=reason))
    return reason


def _total(pid: int, price: float) -> float:
    from app.services import purchase
    row = purchase.get(pid)
    return purchase.costs_of(row, float(price))["total"] if row is not None else float(price)


def _eur(v: float | None) -> str:
    return f"€ {v:.2f}".replace(".", ",") if v is not None else "–"
