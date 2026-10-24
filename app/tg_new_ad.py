"""The Telegram Mini App's new ads (/tg/new-ad): photos to Claude's draft, the draft to willhaben, batches of ads
(the dashboard's ad creation in the Mini App's look; actions via app/tg_bridge.py). Every action of these pages
redirects on the dashboard, so no twin is needed; only the batch table's answer (a fragment of out-of-band rows)
comes back as the batch page without its message."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.tg_web import _page

router = APIRouter()


@router.get("/tg/new-ad", response_class=HTMLResponse)
async def tg_new_ad(request: Request):
    """Start: notes and photos for one item, or all photos of several items; the recent drafts and batches."""
    from app import main
    return await _page(request, "new_ad.html", main.new_ad_page)


@router.get("/tg/new-ad/{draft_id:int}", response_class=HTMLResponse)
async def tg_ad_draft(request: Request, draft_id: int):
    """One draft: Claude's text, its questions and revisions, Geizhals, the willhaben category, handover, publish."""
    from app import main
    return await _page(request, "ad_draft.html", main.ad_draft_page, draft_id, msg=None)


@router.get("/tg/new-ad/batch/{batch_id}", response_class=HTMLResponse)
async def tg_ad_batch(request: Request, batch_id: int):
    """A batch: Claude's groups of photos to correct, then the drafts written from them."""
    from app import main
    return await _page(request, "ad_batch.html", main.ad_batch_page, batch_id, msg=None, unticked="")
