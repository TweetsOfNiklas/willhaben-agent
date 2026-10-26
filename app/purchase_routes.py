"""Kaufauftrag pages: the buy dialog (GET /purchases/new/<ad>) and its POST, the order list and detail, the order
buttons on the chat card and the Settings switch (app/services/purchase.py does the work).

Notices (errors and short confirmations) are kept on the server for a few minutes; the redirect only carries a random
token in `pn`, and base.html shows the text behind it. No text from a link is ever rendered, so a crafted URL shows
nothing of its own.
"""
from __future__ import annotations

import logging
import math
import secrets
import time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import pass_context

from app.db import get_setting, set_setting
from app.i18n import t
from app.platforms import fees
from app.services import auto_buy, deals, price_bar, purchase, shipping
from app.services.purchase import PurchaseError
from app.willhaben.public import WillhabenError, get_public

log = logging.getLogger(__name__)
router = APIRouter()
NOTE_TTL_S = 300
NOTE_CAP = 200
ACTIONS = ("pause", "resume", "cancel", "max_price", "paid", "received", "collected", "restart")
_NOTES: dict[str, tuple[float, str]] = {}


# ------------------------------------------------------------------ notices and redirects
def _flash(text: str) -> str:
    now = time.monotonic()
    for key in [k for k, (at, _) in _NOTES.items() if now - at > NOTE_TTL_S]:
        del _NOTES[key]
    while len(_NOTES) >= NOTE_CAP:
        _NOTES.pop(next(iter(_NOTES)))
    token = secrets.token_urlsafe(9)
    _NOTES[token] = (now, text)
    return token


def _notice_text(token: str | None) -> str | None:
    hit = _NOTES.get(token or "")
    if hit is None or time.monotonic() - hit[0] > NOTE_TTL_S:
        return None
    return hit[1]


@pass_context
def notice(ctx) -> str | None:
    """Jinja global for base.html: the text of the `pn` token of this page's URL, if it is one of ours."""
    request = ctx.get("request")
    return _notice_text(request.query_params.get("pn") if request is not None else None)


def _safe_back(back: str | None, fallback: str) -> str:
    """A page of this app only: relative, no protocol-relative or backslash tricks, no line breaks."""
    b = (back or "").strip()
    if not b.startswith("/") or b.startswith("//") or "\\" in b or "\r" in b or "\n" in b:
        return fallback
    return b


def _go(url: str, text: str | None = None) -> RedirectResponse:
    """303 to a page of ours; the notice travels as a token and the #fragment stays last."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "pn"]
    if text:
        query.append(("pn", _flash(text)))
    return RedirectResponse(urlunsplit(("", "", parts.path, urlencode(query), parts.fragment)), status_code=303)


def _order_page(row) -> str:
    """Where the chat card lives; an order without a chat is shown on its own page."""
    return f"/chats/{row.conversation_id}#purchase" if row.conversation_id else f"/purchases/{row.id}"


# ------------------------------------------------------------------ input parsing (lenient, never raises)
def _float(raw: str | None) -> float | None:
    s = (raw or "").strip()
    if not s:
        return None
    if "," in s:                                   # 1.234,50 or 250,5
        s = s.replace(".", "").replace(",", ".")
    try:
        v = float(s)
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def _int(raw: str | None) -> int | None:
    s = (raw or "").strip()
    return int(s) if s.isdigit() else None


def _https(raw: str | None, prefix: str, limit: int = 500) -> str | None:
    s = (raw or "").strip()
    return s[:limit] if s.startswith(prefix) and not any(c.isspace() for c in s) else None


def _max_price(raw: str | None) -> float | None:
    """Empty is no maximum; anything else must be a positive amount."""
    if not (raw or "").strip():
        return None
    v = _float(raw)
    if v is None or v <= 0:
        raise PurchaseError(t("Enter the maximum price in euros."))
    return v


def _wish_price(raw: str | None) -> float | None:
    """Empty is the suggestion (discount below the asking price); anything else must be a positive amount."""
    if not (raw or "").strip():
        return None
    v = _float(raw)
    if v is None or v <= 0:
        raise PurchaseError(t("Enter the wish price in euros."))
    return v


# ------------------------------------------------------------------ Jinja globals
def enabled() -> bool:
    return get_setting(purchase.SETTING_ENABLED) == "1"


def _discount_value() -> float:
    try:
        return min(max(float(get_setting(purchase.SETTING_DISCOUNT) or purchase.DEFAULT_DISCOUNT_PCT), 0.0), 50.0)
    except ValueError:
        return float(purchase.DEFAULT_DISCOUNT_PCT)


def settings_values() -> dict[str, Any]:
    """Settings → Buying: the switch, the user's rules and the discount (purchase_settings() in _settings_purchase.html)."""
    value = _discount_value()
    on = enabled()
    return {"enabled": on, "rules": get_setting(purchase.SETTING_RULES), "shipping_estimate": purchase.SHIPPING_ESTIMATE,
            "prefer_paylivery": on and get_setting(purchase.SETTING_PREFER_PAYLIVERY) == "1",
            "auto_buy_trust": get_setting(auto_buy.SETTING_TRUST) != "0",
            "discount_pct": int(value) if value == int(value) else value}


# ------------------------------------------------------------------ buy dialog
def _pl_ship(d) -> float:
    """PayLivery delivery price: the ad's own, else the estimate (purchase.start uses the same)."""
    if d.paylivery_enabled and d.shipping_price is not None:
        return d.shipping_price
    return purchase.SHIPPING_ESTIMATE["paylivery"]


async def _market(d) -> dict[str, Any] | None:
    """Price check of the ad (no Claude), the same one as the ad viewer's: the cached analysis when it is for this
    price, else a new one."""
    res = deals.cached(d.ad_id)
    if res is not None and (res.get("ad") or {}).get("price") == d.price:
        return res
    try:
        return await deals.analyze(d)
    except Exception as e:  # noqa: BLE001 - the market data is a bonus; the dialog works without it
        log.info("price check for %s failed: %s", d.ad_id, e)
        return None


def _fair(d, res: dict[str, Any] | None) -> dict[str, Any] | None:
    """The one price-fairness hint of the dialog, from the price check (re-rated now: the current zones): where the
    asking price lies (position, level, basis, its % of the Geizhals new price on the zones basis), how far it is
    from the comparables' median, and what the browser needs to rate the maximum while it is typed and to move it by
    dragging the ring: "js" = the bar's price scale (deals.js_scale), the rule of position() (deals.js_rule: the
    zones and the new price, or the comparables' p10/p25/p75/p90) and the label names. "bar" draws the zones (no
    ring: the needle and the pin are the markers; the dialog names the new-price marker under the bar, "new_price").
    "min"/"max" are the cheapest and the dearest comparable (the ends of a comparables bar). "chips" are the (at most
    three) item prices worth a maximum - cheapest, lower quartile, median - only with 5+ comparables and at least 1 %
    below the asking price. None without a position."""
    res = deals.rerate(res) if res else None
    pos = (res or {}).get("position") or {}
    ask = d.price if d.price and d.price > 0 else None
    if ask is None or pos.get("pos") is None:
        return None
    comp = (res or {}).get("comparables") or {}
    st = comp.get("stats") or {}
    median = st.get("median")
    chips: list[dict[str, Any]] = []
    if deals.anchors(st):
        for key, v in (("min", st["min"]), ("cheap", st["p25"]), ("market", median)):
            if round((ask - v) * 100 / ask) >= 1 and not (chips and round(v) == round(chips[-1]["v"])):
                chips.append({"key": key, "v": round(v, 2)})
    scale, rule = deals.js_scale(pos, st), deals.js_rule(pos, st)
    return {"pos": pos["pos"], "level": pos["level"], "basis": pos.get("basis"), "pct": pos.get("pct"),
            "new_price": pos.get("new_price"), "diff": round((ask - median) * 100 / median) if median else None,
            "count": comp.get("count") or st.get("count"), "chips": chips,
            "median": pos.get("median") if pos.get("basis") == "comparables" else None,
            "bar": price_bar.layout(pos),
            "js": {"pos": pos["pos"], "scale": scale, "rule": rule,
                   "labels": {n: deals.level_label(n) for n in deals.LEVEL_NAMES}} if scale and rule else None}


async def _dialog(ad_id: str, search_id: int | None, query: str | None) -> dict[str, Any]:
    """Everything _buy_dialog.html shows. Nothing is fetched while purchases are switched off."""
    ctx: dict[str, Any] = {"ad_id": ad_id, "search_id": search_id, "query": query or "", "on": enabled(), "d": None,
                           "res": None, "fair": None, "wh_error": None, "gone": False, "ship": {}, "pre": None, "prefill": None,
                           "wish_prefill": None, "discount": _discount_value(), "direct_ok": True,
                           "fee": purchase.fee_rule(), "fee_of": purchase.paylivery_fee}
    if not ctx["on"]:
        return ctx
    try:
        d = await get_public().get_detail(ad_id)
    except WillhabenError as e:
        ctx["wh_error"] = str(e)
        return ctx
    if d is None:
        ctx["gone"] = True
        return ctx
    # PayLivery is preselected only with the preference on and when the ad offers it; the maximum starts at the
    # asking price plus the shipping of that mode and, with PayLivery, the buyer protection fee (no mode: the asking
    # price alone)
    pre = "paylivery" if d.paylivery_enabled and get_setting(purchase.SETTING_PREFER_PAYLIVERY) == "1" else None
    direct_ok = shipping.sender_complete()          # a bank-transfer purchase needs your own address for the parcel
    ways = [m for m, ok in (("paylivery", d.paylivery_enabled), ("direct", direct_ok), ("pickup", True)) if ok]
    if pre is None and len(ways) == 1:              # one way to buy: ticked already
        pre = ways[0]
    ship = {"paylivery": _pl_ship(d), "direct": purchase.SHIPPING_ESTIMATE["direct"], "pickup": 0.0}
    ask = d.price if d.price and d.price > 0 else None
    res = await _market(d)
    rule = fees.rule("willhaben", d.category_path) or purchase.rule_of()    # the fee rule of the ad's category
    from app.willhaben import paylivery_fee
    if d.price and (stated := paylivery_fee.cached(d.ad_id, d.price)) is not None:
        rule = rule.stated(d.price, stated)        # read when the ad was opened (the ad viewer's price breakdown)
    ctx.update(fee=purchase.fee_rule(rule), fee_of=rule.fee)
    extra = rule.fee(ask) if pre == "paylivery" else 0.0
    # the suggested wish price: the discount below the asking price (whole euros) plus shipping and fee of the mode
    # (mirrors wbBuy.wishBase; the agent offers exactly what the field says)
    wish = None
    if ask:
        item = ask * (1 - ctx["discount"] / 100)
        item = float(math.floor(item)) if item >= 1 else round(item, 2)
        wish = round(item + (ship[pre] if pre else 0.0) + (rule.fee(item) if pre == "paylivery" else 0.0), 2)
    ctx.update(d=d, res=res, fair=_fair(d, res), ship=ship, pre=pre, wish_prefill=wish, direct_ok=direct_ok,
               prefill=round(ask + ship[pre] + extra, 2) if (ask and pre) else ask)
    return ctx


@router.get("/purchases/new/{ad_id}", response_class=HTMLResponse)
async def purchase_new(request: Request, ad_id: str, search: str = "", q: str = ""):
    """The buy dialog of one ad, opened from a product search result or the ad viewer. The dialog's script asks
    with X-Requested-With: fetch and gets the fragment; a plain link (no JavaScript) gets the same as a page."""
    from app.main import render, templates

    if not ad_id.isdigit():
        raise HTTPException(400)
    ctx = await _dialog(ad_id, _int(search), q.strip()[:200] or None)
    status = 404 if ctx["gone"] else 200
    if request.headers.get("hx-request") or request.headers.get("x-requested-with") == "fetch":
        return templates.TemplateResponse(request, "_buy_dialog.html", ctx, status_code=status)
    response = render(request, "purchase_new.html", **ctx)
    response.status_code = status
    return response


# ------------------------------------------------------------------ order start
@router.post("/purchases")
async def purchase_start(request: Request, ad_id: str = Form(""), mode: str = Form(""), max_price: str = Form(""),
                         pickup_window: str = Form(""), search_id: str = Form(""), query: str = Form(""),
                         title: str = Form(""), url: str = Form(""), image_url: str = Form(""),
                         price: str = Form(""), back: str = Form(""), max_default: str = Form(""),
                         questions: str = Form(""), wish_price: str = Form("")):
    """Start purchase. Sent through htmx (the dialog) a refusal goes into the dialog's own line (HX-Retarget) and
    the confirmation takes the form's place (_buy_result.html): nothing leaves the page. The order itself starts
    as a job with a grace period (purchase.start), so it can still be taken back. A plain form redirects as before."""
    from app.main import templates

    dest = _safe_back(back, "/purchases")
    hx = bool(request.headers.get("hx-request"))
    try:
        ad = ad_id.strip()
        if not ad.isdigit():
            raise PurchaseError(t("No ad given."))
        pid = purchase.start(
            ad, mode, None if max_default == "1" else _max_price(max_price),   # untouched prefill: asking + shipping (+ fee)
            search_id=_int(search_id), query=query.strip()[:200] or None,
            title=title.strip()[:200] or None, url=_https(url, "https://www.willhaben.at/"),
            image_url=_https(image_url, "https://"), price=_float(price), pickup_window=pickup_window,
            questions=questions, wish_price=_wish_price(wish_price))
    except PurchaseError as e:
        if hx:
            r = templates.TemplateResponse(request, "_buy_result.html", {"error": str(e), "pid": None})
            r.headers.update({"HX-Retarget": "find .bd-error", "HX-Reswap": "innerHTML"})
            return r
        return _go(dest, str(e))
    if hx:
        return templates.TemplateResponse(request, "_buy_result.html", {"error": None, "pid": pid})
    return _go(f"/purchases/{pid}")


@router.post("/purchases/{pid}/{action}")
async def purchase_action(request: Request, pid: int, action: str, value: str = Form("")):
    """One order button. Only the actions the order's current status offers are run (purchase.actions). Sent
    through htmx the card swaps itself in and shows the new state, so only a refusal comes back as a notice."""
    if action not in ACTIONS:
        raise HTTPException(404)
    row = purchase.get(pid)
    if row is None:
        raise HTTPException(404)
    if action not in {a["name"] for a in purchase.actions(row)}:
        return _go(_order_page(row), t("This action is not possible now."))
    text = None if request.headers.get("hx-request") else t("Changes saved")
    try:
        if action == "pause":
            await purchase.pause(pid, t("Paused by you"), push=False)
        elif action == "resume":
            await purchase.resume(pid)
        elif action == "restart":
            await purchase.restart(pid)
        elif action == "cancel":
            purchase.cancel(pid)
        elif action == "max_price":
            maximum = _max_price(value)
            if maximum is None:
                raise PurchaseError(t("Enter the maximum price in euros."))
            purchase.set_max_price(pid, maximum)
        elif action == "paid":
            purchase.mark_paid(pid)
        elif action == "received":
            purchase.mark_received(pid)
        elif action == "collected":
            purchase.mark_collected(pid)
    except PurchaseError as e:
        text = str(e)
    row = purchase.get(pid)
    return _go(_order_page(row) if row is not None else "/purchases", text)


# ------------------------------------------------------------------ pages
def _row_view(p, now_open: set[int]) -> dict[str, Any]:
    tone = {"agreed": "ok", "meetup": "ok", "paid": "ok", "shipped": "ok", "done": "ok", "cancelled": "muted",
            "failed": "muted", "new": "muted", "contacting": "muted", "negotiating": "muted"}.get(p.status, "")
    return {"id": p.id, "title": p.ad_title or p.ad_id, "image": p.image_url, "mode": purchase.mode_label(p.mode),
            "status": purchase.status_label(p.status), "status_key": p.status, "tone": tone, "query": p.query, "asking": p.asking_price,
            "max": p.max_price, "agreed": p.agreed_price, "ad_url": p.ad_url, "open": p.id in now_open,
            "chat": f"/chats/{p.conversation_id}" if p.conversation_id else None,
            "wh_chat": purchase.WH_CHAT.format(cid=p.conversation_id) if p.conversation_id else None}


@router.get("/purchases", response_class=HTMLResponse)
async def purchases_page(request: Request):
    from app.main import render

    return render(request, "purchases.html", rows=_rows(), row=None, pcard=None)


def _rows() -> list[dict[str, Any]]:
    open_rows = purchase.active()
    open_ids = {r.id for r in open_rows}
    return [_row_view(r, open_ids) for r in open_rows + [r for r in purchase.recent(40) if r.id not in open_ids][:20]]


@router.get("/purchases/{pid}", response_class=HTMLResponse)
async def purchase_detail(request: Request, pid: int):
    from app.main import render

    row = purchase.get(pid)
    if row is None:
        raise HTTPException(404)
    return render(request, "purchases.html", rows=_rows(), row=row, pcard=purchase.card_for(row, log_limit=None),
                  chat_url=f"/chats/{row.conversation_id}" if row.conversation_id else None)


# ------------------------------------------------------------------ settings
@router.post("/settings/purchase")
async def settings_purchase(enabled_: str | None = Form(None, alias="enabled"), rules: str | None = Form(None),
                            discount_pct: str = Form(""), prefer_paylivery: str | None = Form(None),
                            auto_buy_trust: str | None = Form(None), trust_seen: str | None = Form(None)):
    raw = _float(discount_pct) if discount_pct.strip() else float(purchase.DEFAULT_DISCOUNT_PCT)
    if raw is None:
        return _go("/settings/purchase", t("Enter a discount between 0 and 50 %."))
    pct = min(max(raw, 0.0), 50.0)
    set_setting(purchase.SETTING_ENABLED, "1" if enabled_ else "0")
    set_setting(purchase.SETTING_PREFER_PAYLIVERY, "1" if (enabled_ and prefer_paylivery) else "0")   # only with orders on
    if rules is not None:          # edited under Settings → AI rules now; a form without it keeps them
        set_setting(purchase.SETTING_RULES, rules.strip()[:4000])
    set_setting(purchase.SETTING_DISCOUNT, f"{pct:g}")
    if trust_seen:                 # the checkbox sends nothing when off
        set_setting(auto_buy.SETTING_TRUST, "1" if auto_buy_trust else "0")
    return _go("/settings/purchase", t("Changes saved"))
