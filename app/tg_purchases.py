"""The Telegram Mini App's purchase orders (/tg/purchases): the list, one order, a new order for an ad (the
dashboard's purchase pages in the Mini App's look). The order's buttons and Start run the dashboard's own handlers
(app/purchase_routes.py) through the small wrappers below: their notices travel as a server-side token (`pn`), which
the wrappers turn into the Mini App's message, and an order keeps its own page (the dashboard sends one with a chat
to the chat)."""
from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from app import purchase_routes
from app.i18n import t
from app.services import purchase
from app.tg_web import _back, _page, _render

router = APIRouter()


def _ad_id(ref: str) -> str | None:
    """The willhaben id of an ad ref ("123" or "willhaben:123"); None for other sites (orders are willhaben's)."""
    pid, _, rest = ref.partition(":")
    ad = rest if rest else pid
    if rest and pid != "willhaben":
        return None
    return ad if ad.isdigit() else None


def _with_msg(path: str, text: str | None) -> str:
    return f"{path}?msg={quote(text, safe='')}" if text else path


def _notice(resp: Any) -> str | None:
    """The dashboard's notice behind the redirect's `pn` token (purchase_routes keeps the text on the server)."""
    token = (parse_qs(urlsplit(resp.headers.get("location", "")).query).get("pn") or [None])[0]
    return purchase_routes._notice_text(token)


@router.get("/tg/purchases", response_class=HTMLResponse)
async def tg_purchases(request: Request, show: str = "open"):
    """The dashboard's list: ongoing orders, the finished ones, or all."""
    return _render(request, "purchases.html", rows=purchase_routes._rows(),
                   show=show if show in ("open", "done", "all") else "open")


@router.get("/tg/purchases/new/{ad_ref:path}", response_class=HTMLResponse)
async def tg_purchase_new(request: Request, ad_ref: str, search: str = "", q: str = ""):
    """The buy dialog of one ad (purchase_routes._dialog): how to buy, wish price and maximum, questions, Start."""
    ad = _ad_id(ad_ref)
    if ad is None:
        return _render(request, "purchase_new.html", on=True, d=None, gone=False, wh_error=None, ad_id=ad_ref,
                       other_site=True)
    return await _page(request, "purchase_new.html", purchase_routes.purchase_new, ad, search=search, q=q)


@router.post("/tg/purchases/new/{ad_ref:path}")
async def tg_purchase_start(request: Request, ad_ref: str, mode: str = Form(""), max_price: str = Form(""),
                            pickup_window: str = Form(""), search_id: str = Form(""), query: str = Form(""),
                            title: str = Form(""), url: str = Form(""), image_url: str = Form(""),
                            price: str = Form(""), max_default: str = Form(""), questions: str = Form(""),
                            wish_price: str = Form("")):
    """Start: the dashboard's handler; the new order opens, a refusal comes back to the dialog."""
    ad = _ad_id(ad_ref) or ""
    here = f"/tg/purchases/new/{ad_ref}"
    resp = await purchase_routes.purchase_start(
        request, ad_id=ad, mode=mode, max_price=max_price, pickup_window=pickup_window, search_id=search_id,
        query=query, title=title, url=url, image_url=image_url, price=price, back=here, max_default=max_default,
        questions=questions, wish_price=wish_price)
    ctx = getattr(resp, "context", None)
    if ctx is not None:                      # sent with htmx: the result fragment's context
        if ctx.get("pid"):
            return _back(_with_msg(f"/tg/purchases/{ctx['pid']}", t("Purchase started")))
        return _back(_with_msg(here, ctx.get("error")))
    path = urlsplit(resp.headers.get("location", "")).path
    if path.startswith("/purchases/") and path.rsplit("/", 1)[-1].isdigit():
        return _back(_with_msg(f"/tg{path}", t("Purchase started")))
    return _back(_with_msg(here, _notice(resp)))


@router.get("/tg/purchases/{pid}", response_class=HTMLResponse)
async def tg_purchase(request: Request, pid: int):
    row = purchase.get(pid)
    if row is None:
        raise HTTPException(404)
    return _render(request, "purchase.html", row=row, pcard=purchase.card_for(row, log_limit=None))


@router.post("/tg/purchases/{pid}/{action}")
async def tg_purchase_action(request: Request, pid: int, action: str, value: str = Form("")):
    """One order button (the dashboard's handler, only what the status offers); back to the order with its notice."""
    resp = await purchase_routes.purchase_action(request, pid, action, value)
    return _back(_with_msg(f"/tg/purchases/{pid}", _notice(resp)))
