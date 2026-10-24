"""The Telegram Mini App's new ads (/tg/new-ad): photos to Claude's draft, the draft to willhaben, batches of ads
(the dashboard's ad creation in the Mini App's look; actions via app/tg_bridge.py). Every action of these pages
redirects on the dashboard, so no twin is needed; the batch table's answer (a fragment of out-of-band rows) is
taken apart by tg_ad_batch_drafts here: the batch page with the table's message."""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

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
async def tg_ad_batch(request: Request, batch_id: int, unticked: str = ""):
    """A batch: Claude's groups of photos to correct, then the drafts written from them. unticked: drafts unticked
    before the last action (they stay unticked)."""
    from app import main
    return await _page(request, "ad_batch.html", main.ad_batch_page, batch_id, msg=None, unticked=unticked)


@router.post("/tg/new-ad/batch/{batch_id}/drafts")
async def tg_ad_batch_drafts(request: Request, batch_id: int):
    """The batch table's save and its actions on the ticked drafts (or one): the dashboard's handler saves every row
    and runs the action; its answer is a fragment for the dashboard's table, so its message ("Saved", a refusal to
    publish) comes back with the Mini App's batch page, the review step open."""
    from app import main
    resp = await main.ad_batch_drafts(request, batch_id)
    msg = (getattr(resp, "context", None) or {}).get("msg") or ""
    unticked = [v for v in (await request.form()).getlist("all") if v not in (await request.form()).getlist("sel")]
    url = f"/tg/new-ad/batch/{batch_id}?step=review" + (f"&unticked={','.join(unticked)}" if unticked else "")
    return RedirectResponse(url + (f"&msg={quote(str(msg), safe='')}" if msg else ""), status_code=303)
