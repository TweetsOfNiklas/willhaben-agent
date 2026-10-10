"""Settings → Printers and the print buttons on the chat page (services/printing.py does the work)."""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.i18n import t
from app.services import printing, shipping

router = APIRouter()


def panel() -> dict:
    """Template context for _settings_printing.html."""
    return {"printing": printing.config(), "print_opts": printing.form_options()}


def shipment_panel(shipment) -> dict:
    """Template context for _shipping_print.html: the print buttons, and the printer's state of the job if printed
    within a day. The state comes from the last answer and is refreshed in the background (no waiting here)."""
    return {"printing": printing.config(),
            "print_choice": printing.choice_for(shipment),
            "print_state": printing.cached_job_state(shipment.print_job, shipment.printed_at) if shipment else None}


def _settings_back(msg: str) -> RedirectResponse:
    return RedirectResponse(f"/settings?msg={quote(msg, safe='')}#printers", status_code=303)


@router.post("/settings/printers")
async def save_printers(request: Request):
    form = await request.form()
    try:
        printing.save({k: v for k, v in form.items() if isinstance(v, str)})
    except printing.PrintError as e:
        return _settings_back(str(e))
    return _settings_back(t("Printer settings saved"))


@router.post("/settings/printers/test/{which}")
async def test_printer(which: str):
    try:
        job = await printing.test_page(which)
    except printing.PrintError as e:
        return _settings_back(str(e))
    return _settings_back(t("Test page sent to the {printer} (job {job})", printer=printing.printer_name(which), job=job))


@router.post("/settings/printers/check/{which}")
async def check_printer(which: str):
    try:
        report = await printing.check(which)
    except printing.PrintError as e:
        return _settings_back(str(e))
    return _settings_back(report)


@router.post("/shipments/{shipment_id}/print")
async def print_shipment(shipment_id: int, which: str = Form("auto")):
    if which not in ("auto", "label", "normal"):
        raise HTTPException(400)
    s = shipping.get(shipment_id)
    if s is None:
        raise HTTPException(404)
    try:
        msg = t("Label sent to the printer (job {job})", job=await printing.print_label(shipment_id, which))
    except printing.PrintError as e:
        msg = str(e)
    return RedirectResponse(f"/chats/{quote(s.conversation_id, safe='')}?msg={quote(msg, safe='')}#shipping",
                            status_code=303)
