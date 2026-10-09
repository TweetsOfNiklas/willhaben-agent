"""Buy the parcel label on the carrier's website without the user (stub: built by the "label purchase" agent).

Contract used by `shipping` (the pipeline) and the chat page:
- `async buy(shipment_id) -> Shipment`: runs the carrier's checkout in the remote browser for an approved shipment
  (status "buying"). On success the shipment is "labelled" with `label_path` and `paid_price` set and `buy_log` filled.
  Raises `NeedsHuman` when a step needs the user (payment confirmation, unknown page): the page stays open in the
  remote view, `error` says what to do. Raises `BuyError` for everything else (status "failed", `error` set).
- `card()` / `save_card()` / `card_status()`: the payment card typed into the carrier's payment form, stored in
  DATA_DIR (mode 600) like the carrier logins; never sent to Claude, never shown again.
- `active()`: the open checkout page for the session page, like `label_fill.active()`.
"""
from __future__ import annotations

from typing import Any


class BuyError(RuntimeError):
    pass


class NeedsHuman(BuyError):
    """The user has to finish a step in the remote view (e.g. confirm the payment with the bank)."""


def card_status() -> dict[str, Any]:
    """For the settings page: {"stored": bool, "last4": str, "holder": str, "expiry": str} - never the number."""
    return {"stored": False, "last4": "", "holder": "", "expiry": ""}


def active() -> dict[str, Any] | None:
    return None


async def buy(shipment_id: int):
    raise BuyError("label purchase is not built yet")
