"""Settings → Shipping → payment card, and the checkout's step button on the session page."""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Form
from fastapi.responses import RedirectResponse

from app.i18n import t
from app.services import label_buy
from app.services.shipping_tariffs import CARRIERS

router = APIRouter()


def panel() -> dict:
    """Template context for _settings_ship_card.html."""
    return {"card": label_buy.card_status(), "playbooks": label_buy.playbook_status()}


def _settings(msg: str) -> RedirectResponse:
    return RedirectResponse(f"/settings?msg={quote(msg, safe='')}#shipping", status_code=303)


def _session(next_url: str | None, msg: str) -> RedirectResponse:
    url = next_url if next_url and next_url.startswith("/") and not next_url.startswith("//") else "/session"
    path, _, frag = url.partition("#")
    path += ("&" if "?" in path else "?") + "msg=" + quote(msg, safe="")
    return RedirectResponse(path + (f"#{frag}" if frag else ""), status_code=303)


@router.post("/settings/shipping/card")
async def settings_card_save(holder: str = Form(""), number: str = Form(""), expiry: str = Form(""),
                             cvc: str = Form("")):
    try:
        label_buy.save_card(holder, number, expiry, cvc)
    except label_buy.CardError as e:
        return _settings(str(e))
    return _settings(t("Payment card saved"))


@router.post("/settings/shipping/card/delete")
async def settings_card_delete():
    label_buy.remove_card()
    return _settings(t("Payment card removed"))


@router.post("/settings/shipping/playbook/{carrier}/reset")
async def settings_playbook_reset(carrier: str):
    if carrier not in CARRIERS:
        return _settings(t("unknown carrier"))
    label_buy.reset_playbook(carrier)
    return _settings(t("Playbook reset – the next purchase asks Claude again"))


@router.post("/session/label-buy/step")
async def session_label_buy_step(next: str = Form("/session")):
    try:
        msg = await label_buy.step_by_hand()
    except label_buy.BuyError as e:
        msg = str(e)
    return _session(next, msg)
