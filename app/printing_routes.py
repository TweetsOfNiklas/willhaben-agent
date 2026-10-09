"""Settings → Printers and the print buttons on the chat page (built by the "printing" agent)."""
from __future__ import annotations

from fastapi import APIRouter

from app.services import printing

router = APIRouter()


def panel() -> dict:
    """Template context for _settings_printing.html."""
    return {"printing": printing.config()}


def shipment_panel(shipment) -> dict:
    """Template context for _shipping_print.html (the print button of one shipment)."""
    return {"printing": printing.config()}
