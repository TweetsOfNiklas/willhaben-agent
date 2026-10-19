"""PayLivery in the app: the card's action buttons on the chat page and the switches in Settings.

Browser actions start in the background (paylivery.start_action); the chat page shows the result in the card.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app.i18n import t
from app.services import paylivery

router = APIRouter()


def card(conv) -> dict[str, Any] | None:
    """Template context for _paylivery.html (Jinja global `paylivery_card`). conv None: the settings values."""
    return paylivery.card(conv)


def _back(path: str, msg: str, anchor: str) -> RedirectResponse:
    return RedirectResponse(f"{path}?msg={quote(msg, safe='')}{anchor}", status_code=303)


@router.post("/chats/{conversation_id}/paylivery/{action}")
async def chat_paylivery_action(request: Request, conversation_id: str, action: str, amount: str = Form("")):
    """Sent through htmx the card swaps itself in and shows the state (working in the browser…, shipped), so only
    a refusal comes back as a message; a plain form gets the confirmation too."""
    try:
        msg = await paylivery.start_action(conversation_id, action, amount)
        if request.headers.get("hx-request"):
            msg = ""
    except paylivery.PayLiveryError as e:
        msg = str(e)
    return _back(f"/chats/{conversation_id}", msg, "#paylivery")


@router.post("/settings/paylivery")
async def settings_paylivery(notify: str | None = Form(None)):
    paylivery.save_settings(notify=bool(notify))
    return _back("/settings/paylivery", t("Changes saved"), "")
