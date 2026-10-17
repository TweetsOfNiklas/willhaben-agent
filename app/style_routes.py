"""Settings → Selling → My rules → Writing style: derive the profile from my messages (app/services/style.py)."""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.i18n import t
from app.services import style

router = APIRouter()


@router.post("/settings/style/derive", response_class=HTMLResponse)
async def style_derive(request: Request):
    """Start the derivation in the background. The card (htmx) comes back at once with its running state and
    refreshes itself when the job ends; without JavaScript the page reloads with a short message."""
    sp = style.panel()
    if not sp["enough"]:
        note = t("Only {n} of your own messages so far – at least {min} are needed", n=sp["count"], min=sp["min"])
    elif style.start() is None:
        note = t("Claude is already deriving your writing style")     # the card shows the running state anyway
    else:
        note = t("Claude derives your writing style in the background")
    if request.headers.get("hx-request"):
        from app.main import templates

        return templates.TemplateResponse(request, "settings/_style.html", {})
    return RedirectResponse(f"/settings/rules?msg={quote(note, safe='')}", status_code=303)
