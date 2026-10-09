"""Settings → Shipping → payment card, and the checkout's buttons on the session page (built by the "label purchase" agent)."""
from __future__ import annotations

from fastapi import APIRouter

from app.services import label_buy

router = APIRouter()


def panel() -> dict:
    """Template context for _settings_ship_card.html."""
    return {"card": label_buy.card_status()}
