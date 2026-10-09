"""Send label PDFs to a label printer or a normal printer over IPP (stub: built by the "printing" agent).

Contract used by `shipping` (the pipeline), the chat page and the settings page:
- `config() -> dict`: {"auto": bool, "printers": {"label": {...}, "normal": {...}}, "by_carrier": {carrier: "label"|"normal"|"none"}}.
- `enabled() -> bool`: automatic printing is on and the needed printer is configured.
- `async print_label(shipment_id) -> str`: prints the shipment's label on the printer chosen for its carrier
  (cropped/scaled to the label medium for label printers, fit to page for normal printers), stores `print_job` and
  `printed_at`, sets status "printed" when it was "labelled". Returns the job id. Raises `PrintError`.
- `async test_page(which) -> str`: prints a test page on "label" or "normal".
"""
from __future__ import annotations

from typing import Any


class PrintError(RuntimeError):
    pass


def config() -> dict[str, Any]:
    return {"auto": False, "printers": {}, "by_carrier": {}}


def enabled() -> bool:
    return False


async def print_label(shipment_id: int) -> str:
    raise PrintError("printing is not built yet")


async def test_page(which: str) -> str:
    raise PrintError("printing is not built yet")
