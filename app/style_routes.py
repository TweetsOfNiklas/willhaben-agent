"""Settings → Selling → My rules → Writing style: derive the profile from my messages (app/services/style.py)."""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.i18n import t
from app.services import style

router = APIRouter()


@router.get("/settings/style/card", response_class=HTMLResponse)
async def style_card(request: Request):
    """The card alone (settings/_style.html): what an open rules page fetches to refresh it (data-refresh), instead of
    the whole settings page with its connection checks."""
    from app.main import templates

    return templates.TemplateResponse(request, "settings/_style.html", {})


@router.post("/settings/style/derive", response_class=HTMLResponse)
async def style_derive(request: Request):
    """Start the derivation in the background. The card (htmx) comes back at once with its running state and
    refreshes itself when the job ends; without JavaScript the page reloads with a short message."""
    count = len(style.own_messages())
    started = count >= style.MIN_MESSAGES and style.start() is not None
    if request.headers.get("hx-request"):
        from app.main import templates

        return templates.TemplateResponse(request, "settings/_style.html", {})   # the card reads the state itself
    if count < style.MIN_MESSAGES:
        note = t("Only {n} of your own messages so far – at least {min} are needed", n=count, min=style.MIN_MESSAGES)
    elif not started:
        note = t("Claude is already deriving your writing style")
    else:
        note = t("Claude derives your writing style in the background")
    return RedirectResponse(f"/settings/rules?msg={quote(note, safe='')}", status_code=303)
