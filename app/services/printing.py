"""Send label PDFs to a label printer or a normal printer over IPP (services/ipp.py, pure Python).

Contract used by `shipping` (the pipeline), the chat page and the settings page:
- `config() -> dict`: {"auto": bool, "printers": {"label": {...}, "normal": {...}}, "by_carrier": {carrier: "label"|"normal"|"none"}}.
  `by_carrier` also has "paylivery": willhaben's PayLivery labels, whose carrier this app does not know.
- `auto_choice(shipment) -> str | None`: the printer a label goes to on its own, or None. One switch ("auto") covers
  every label: bought here, downloaded from a carrier form, or sent by willhaben PayLivery. `auto_missing(shipment)`
  says why a label is not printed although the switch is on (its printer has no address).
- `async print_label(shipment_id, which="auto") -> str`: prints the shipment's label on the printer chosen for its carrier
  (or on `which`). Label printers get the first page cropped to the label and fitted to the medium, normal printers the
  PDF as it is, fitted to the page. Stores `print_job` and `printed_at`, sets status "printed" when it was "labelled".
  Returns the job id. Raises `PrintError`.
- `async test_page(which) -> str`: prints a test page on "label" or "normal".
- `async check(which) -> str`: asks the printer what it supports (state, media, trays) and returns a short report.
"""
from __future__ import annotations

import asyncio
import copy
import io
import json
import logging
import re
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, RectangleObject

from app.config import get_settings
from app.db import get_setting, set_setting, utcnow
from app.i18n import t
from app.services import ipp
from app.services.shipping_tariffs import CARRIERS

log = logging.getLogger(__name__)
_transport: httpx.AsyncBaseTransport | None = None  # tests inject a mock transport

PRINTERS = ("label", "normal")
CHOICES = ("label", "normal", "none")
CROP_KEYS = ("left", "top", "width", "height")
MEDIA = {"label": ["custom_100x150mm_100x150mm", "custom_100x200mm_100x200mm"], "normal": ["iso_a4_210x297mm", "iso_a5_148x210mm"]}
MEDIA_NAMES = {"custom_100x150mm_100x150mm": "100 × 150 mm", "custom_100x200mm_100x200mm": "100 × 200 mm",
               "iso_a4_210x297mm": "A4", "iso_a5_148x210mm": "A5"}
TRAYS = ["auto", "tray-1", "tray-2", "tray-3", "manual", "by-pass"]
PAYLIVERY = "paylivery"     # printer row for willhaben's PayLivery labels (format unknown: normal printer, as it is)
DEFAULTS: dict[str, Any] = {
    "auto": False,
    "printers": {
        "label": {"name": "", "uri": "", "media": MEDIA["label"][0], "tray": "auto", "carriers": {}},
        "normal": {"name": "", "uri": "", "media": MEDIA["normal"][0], "tray": "auto"},
    },
    "by_carrier": {"post": "normal", "gls": "label", "hermes": "label", "dpd": "label", PAYLIVERY: "normal"},
}
_KEYWORD = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,79}$")
_URI = re.compile(r"^(ipps?|https?)://[^\s/]+", re.I)


class PrintError(RuntimeError):
    pass


# ------------------------------------------------------------------ settings
def _raw() -> dict[str, Any]:
    try:
        data = json.loads(get_setting("printers") or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def config() -> dict[str, Any]:
    raw = _raw()
    cfg = copy.deepcopy(DEFAULTS)
    cfg["auto"] = bool(raw.get("auto", False))
    printers = raw.get("printers") if isinstance(raw.get("printers"), dict) else {}
    for which in PRINTERS:
        given = printers.get(which)
        if isinstance(given, dict):
            cfg["printers"][which].update({k: v for k, v in given.items() if k in cfg["printers"][which]})
    by_carrier = raw.get("by_carrier") if isinstance(raw.get("by_carrier"), dict) else {}
    for code, choice in by_carrier.items():
        if code in cfg["by_carrier"] and choice in CHOICES:
            cfg["by_carrier"][code] = choice
    carriers = cfg["printers"]["label"]["carriers"]
    cfg["printers"]["label"]["carriers"] = {k: v for k, v in (carriers if isinstance(carriers, dict) else {}).items()
                                            if k in CARRIERS and isinstance(v, dict)}
    return cfg


def media_for(cfg: dict[str, Any], carrier: str) -> str:
    """The medium a label of this carrier is printed on: the carrier's own medium on the label printer, else the
    printer's. GLS labels are 100 x 200 mm, so GLS uses that when the printer is set to 100 x 150 mm."""
    label = cfg["printers"]["label"]
    own = label["carriers"].get(carrier, {}).get("media")
    if own:
        return own
    if carrier == "gls" and label["media"] == MEDIA["label"][0]:
        return MEDIA["label"][1]
    return label["media"]


def _crop_of(cfg: dict[str, Any], carrier: str) -> dict[str, float] | None:
    return cfg["printers"]["label"]["carriers"].get(carrier, {}).get("crop")


def carrier_of(shipment) -> str:
    """The printer row of a shipment: "paylivery" for willhaben's labels, else the carrier of its tariff key."""
    if shipment is None:
        return ""
    if shipment.paid_via == "paylivery":
        return PAYLIVERY
    return (shipment.option or "").split(":", 1)[0]


def choice_for(shipment) -> str:
    """Which printer the shipment's label goes to: label, normal or none."""
    return config()["by_carrier"].get(carrier_of(shipment), "none")


def auto_choice(shipment) -> str | None:
    """The printer to print this label on without asking, or None: automatic printing off, "No printing" for its
    carrier, or that printer has no address yet (then the print buttons on the chat page say what is missing)."""
    cfg = config()
    choice = cfg["by_carrier"].get(carrier_of(shipment), "none")
    if not cfg["auto"] or choice not in PRINTERS or not cfg["printers"][choice].get("uri"):
        return None
    return choice


def auto_missing(shipment) -> str | None:
    """Why automatic printing is on but this label is not printed: its printer has no address (None otherwise)."""
    cfg = config()
    choice = cfg["by_carrier"].get(carrier_of(shipment), "none")
    if cfg["auto"] and choice in PRINTERS and not cfg["printers"][choice].get("uri"):
        return t("Not printed: the {printer} has no address – Settings → Printers", printer=printer_name(choice))
    return None


def printer_name(which: str) -> str:
    return t("Label printer") if which == "label" else t("Normal printer")


def _tray_label(value: str) -> str:
    return {"auto": t("automatic"), "manual": t("manual feed"), "by-pass": t("bypass")}.get(value, value)


def form_options() -> dict[str, Any]:
    """Choices for the settings form: values with their labels."""
    return {
        "media": {w: [(m, MEDIA_NAMES.get(m, m)) for m in MEDIA[w]] for w in PRINTERS},
        "trays": [(v, _tray_label(v)) for v in TRAYS],
        "carriers": [(code, c.name) for code, c in CARRIERS.items()],
        "targets": [(code, c.name) for code, c in CARRIERS.items()] + [(PAYLIVERY, "willhaben PayLivery")],
        "choices": [("label", t("Label printer")), ("normal", t("Normal printer")), ("none", t("No printing"))],
        "carrier_media": [("", t("printer default"))] + [(m, MEDIA_NAMES.get(m, m)) for m in MEDIA["label"]]
                         + [("other", t("Other (type below)"))],
    }


def _valid(value: str) -> str:
    if not _KEYWORD.match(value):
        raise PrintError(t("Enter a valid media or tray name, e.g. custom_100x150mm_100x150mm."))
    return value


def _name(form: Mapping[str, str], select: str, other: str, default: str) -> str:
    value = form.get(select) or default
    if value == "other":
        value = (form.get(other) or "").strip()
    return _valid(value)


def _uri(value: str) -> str:
    v = value.strip()
    if v and not _URI.match(v):
        raise PrintError(t("The printer address must start with ipp:// (e.g. ipp://192.168.1.50/ipp/print)."))
    return v


def _crop_for(form: Mapping[str, str], code: str, name: str) -> dict[str, float] | None:
    raw = [(form.get(f"crop_{code}_{k}") or "").strip().replace(",", ".") for k in CROP_KEYS]
    if not any(raw):
        return None
    message = t("Crop for {carrier}: enter all four values as fractions between 0 and 1.", carrier=name)
    try:
        left, top, width, height = (float(v) for v in raw)
    except ValueError as e:
        raise PrintError(message) from e
    if not (0 <= left < 1 and 0 <= top < 1 and 0 < width <= 1 - left + 1e-9 and 0 < height <= 1 - top + 1e-9):
        raise PrintError(message)
    return dict(zip(CROP_KEYS, (left, top, width, height), strict=True))


def _carriers(form: Mapping[str, str]) -> dict[str, dict[str, Any]]:
    """Per carrier on the label printer: its medium (empty: the printer's) and its crop (empty: none)."""
    out: dict[str, dict[str, Any]] = {}
    for code, carrier in CARRIERS.items():
        entry: dict[str, Any] = {}
        media = form.get(f"label_carrier_media_{code}") or ""
        if media == "other":
            entry["media"] = _valid((form.get(f"label_carrier_media_{code}_other") or "").strip())
        elif media:
            entry["media"] = _valid(media)
        crop = _crop_for(form, code, carrier.name)
        if crop:
            entry["crop"] = crop
        if entry:
            out[code] = entry
    return out


def save(form: Mapping[str, str]) -> None:
    """Validate the printer settings form and store it. Raises PrintError with the first problem."""
    cfg = copy.deepcopy(DEFAULTS)
    cfg["auto"] = bool(form.get("auto"))
    for which in PRINTERS:
        p = cfg["printers"][which]
        p["name"] = (form.get(f"{which}_name") or "").strip()[:80]
        p["uri"] = _uri(form.get(f"{which}_uri") or "")
        p["media"] = _name(form, f"{which}_media", f"{which}_media_other", p["media"])
        p["tray"] = _name(form, f"{which}_tray", f"{which}_tray_other", "auto")
    cfg["printers"]["label"]["carriers"] = _carriers(form)
    cfg["by_carrier"] = {code: form.get(f"carrier_{code}") or default for code, default in DEFAULTS["by_carrier"].items()}
    for choice in cfg["by_carrier"].values():
        if choice not in CHOICES:
            raise PrintError(t("Choose a printer for each carrier."))
    set_setting("printers", json.dumps(cfg, ensure_ascii=False))


# ------------------------------------------------------------------ PDF
def label_pdf(data: bytes, media: str, crop: dict[str, float] | None = None) -> bytes:
    """First page, cropped to the label, turned to the medium's orientation and fitted onto the medium.

    The result has no /Rotate and its boxes are the medium, and the crop is a clip path in the content, so a printer
    that ignores boxes or rotation still prints the label where it belongs. Anything outside the crop is clipped.
    """
    try:
        reader = PdfReader(io.BytesIO(data))
        writer = PdfWriter()
        page = writer.add_page(reader.pages[0])
    except Exception as e:  # pypdf raises many kinds of errors for bad files
        raise PrintError(t("The label is not a readable PDF.")) from e
    page.transfer_rotation_to_content()
    box = page.cropbox
    x0, y0, x1, y1 = (float(v) for v in (box.left, box.bottom, box.right, box.top))
    if crop:
        w, h = x1 - x0, y1 - y0
        cx0 = x0 + crop["left"] * w
        cy1 = y1 - crop["top"] * h
        x0, y0, x1, y1 = cx0, cy1 - crop["height"] * h, cx0 + crop["width"] * w, cy1
    cw, ch = x1 - x0, y1 - y0
    size = ipp.media_size_mm(media)
    tw, th = (v * 72 / 25.4 for v in size) if size else (cw, ch)
    turn = size is not None and (cw > ch) != (tw > th)
    fw, fh = (ch, cw) if turn else (cw, ch)                       # content size after turning
    s = min(tw / fw, th / fh)
    ox, oy = (tw - s * fw) / 2, (th - s * fh) / 2
    if turn:                                                      # (x, y) -> (y, -x): a quarter turn clockwise
        ctm = (0, -s, s, 0, ox - s * y0, oy + s * (cw + x0))
    else:
        ctm = (s, 0, 0, s, ox - s * x0, oy - s * y0)
    original = page.get_contents()
    content = original.get_data() if original is not None else b""
    stream = DecodedStreamObject()
    stream.set_data(f"q {x0:.4f} {y0:.4f} {cw:.4f} {ch:.4f} re W n ".encode()
                    + ("{:.6f} {:.6f} {:.6f} {:.6f} {:.4f} {:.4f} cm\n".format(*ctm)).encode()
                    + content + b"\nQ\n")
    page.replace_contents(stream)
    page.mediabox = RectangleObject((0, 0, tw, th))
    page.cropbox = RectangleObject((0, 0, tw, th))
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def test_page_pdf(media: str) -> bytes:
    """One page of the medium's size: a frame 3 mm inside the edge (shows clipping and offsets) and a text."""
    w, h = (v * 72 / 25.4 for v in (ipp.media_size_mm(media) or (210, 297)))
    m = 3 * 72 / 25.4
    size = ipp.media_size_mm(media) or (210, 297)
    content = (f"{m:.2f} {m:.2f} {w - 2 * m:.2f} {h - 2 * m:.2f} re S\n"
               f"BT /F1 14 Tf {m + 6:.2f} {h - m - 20:.2f} Td (SecondHandler test page) Tj ET\n"
               f"BT /F1 10 Tf {m + 6:.2f} {h - m - 36:.2f} Td ({size[0]:g} x {size[1]:g} mm, {media}) Tj ET\n")
    writer = PdfWriter()
    page = writer.add_blank_page(width=w, height=h)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
    stream = DecodedStreamObject()
    stream.set_data(content.encode("latin-1"))
    page.replace_contents(stream)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


# ------------------------------------------------------------------ printing
def _ready(cfg: dict[str, Any], which: str) -> dict[str, Any]:
    if which not in PRINTERS:
        raise PrintError(t("Unknown printer."))
    printer = cfg["printers"][which]
    if not printer.get("uri"):
        raise PrintError(t("The {printer} has no address yet. Enter it under Settings → Printers.",
                           printer=printer_name(which)))
    return printer


async def _send(printer: dict[str, Any], document: bytes, job_name: str, media: str | None = None) -> int:
    try:
        return await ipp.print_job(printer["uri"], document, job_name=job_name, media=media or printer["media"],
                                   tray=printer.get("tray"), transport=_transport)
    except ipp.IPPError as e:
        raise PrintError(str(e)) from e


async def print_label(shipment_id: int, which: str = "auto") -> str:
    from app.services import shipping      # shipping calls this module, so import it late

    s = shipping.get(shipment_id)
    if s is None:
        raise PrintError(t("Shipment not found"))
    if not s.label_path:
        raise PrintError(t("No label to print yet."))
    cfg = config()
    carrier = carrier_of(s)
    choice = cfg["by_carrier"].get(carrier, "none") if which == "auto" else which
    if choice not in PRINTERS:
        raise PrintError(t("No printer is set for this carrier. Choose one under Settings → Printers."))
    try:
        printer = _ready(cfg, choice)
        path = get_settings().data_dir / "labels" / Path(s.label_path).name
        if not path.exists():
            raise PrintError(t("The label file is missing."))
        data = path.read_bytes()
        media = None
        if choice == "label":
            media = media_for(cfg, carrier)
            data = label_pdf(data, media, _crop_of(cfg, carrier))
        job = await _send(printer, data, f"willhaben {shipment_id}", media)
    except PrintError as e:
        shipping.update(shipment_id, error=str(e))           # shown on the chat page; the status stays
        raise
    fields: dict[str, Any] = {"print_job": f"{printer['uri']}#{job}", "printed_at": utcnow(), "error": None}
    if s.status == "labelled":
        fields["status"] = "printed"
    shipping.update(shipment_id, **fields)
    log.info("shipment %s printed on the %s printer (job %s)", shipment_id, choice, job)
    return str(job)


async def test_page(which: str) -> str:
    printer = _ready(config(), which)
    return str(await _send(printer, test_page_pdf(printer["media"]), "willhaben test page"))


async def check(which: str) -> str:
    printer = _ready(config(), which)
    try:
        msg = await ipp.printer_attributes(printer["uri"], transport=_transport)
    except ipp.IPPError as e:
        raise PrintError(str(e)) from e
    states = {"idle": t("idle"), "processing": t("printing"), "stopped": t("stopped")}
    state = states.get(ipp.PRINTER_STATES.get(msg.get("printer-state"), ""), t("unknown"))
    reasons = [r for r in msg.values("printer-state-reasons") if r != "none"]
    media = msg.values("media-supported")
    formats = msg.values("document-format-supported")
    if not formats:
        pdf = t("not reported")
    else:
        pdf = t("yes") if "application/pdf" in formats else t("no")
    shown = ", ".join(media[:12]) + (" …" if len(media) > 12 else "")
    return t("{printer}: {state}, reasons: {reasons}. PDF: {pdf}. Media: {media}. Trays: {trays}.",
             printer=printer_name(which), state=state, reasons=", ".join(reasons) or t("none"), pdf=pdf,
             media=shown or t("not reported"), trays=", ".join(msg.values("media-source-supported")) or t("not reported"))


# ------------------------------------------------------------------ job state (chat page)
JOB_TTL_S = 30          # how long a fetched job state is reused
JOB_TIMEOUT_S = 3       # the printer gets this long to answer
RECENT = 24 * 3600      # only jobs printed this recently are asked about
_job_states: dict[str, tuple[float, str]] = {}      # print_job -> (monotonic time, IPP job state or "unknown")
_refreshing: set[str] = set()
_tasks: set[asyncio.Task] = set()


async def job_state(print_job: str) -> str:
    """The printer's state of a printed job ("uri#id"); "unknown" when it cannot be asked in time."""
    uri, _, job = print_job.rpartition("#")
    try:
        return await asyncio.wait_for(ipp.job_state(uri, int(job), transport=_transport), JOB_TIMEOUT_S)
    except Exception:  # noqa: BLE001 - the printer may be off or slow; the chat page only shows a hint
        return "unknown"


def job_label(state: str) -> str:
    return {"pending": t("waiting"), "pending-held": t("held"), "processing": t("in progress"),
            "processing-stopped": t("stopped"), "canceled": t("canceled"), "aborted": t("aborted"),
            "completed": t("completed"), "unknown": t("unknown")}.get(state, state)


async def _refresh(print_job: str) -> None:
    try:
        _job_states[print_job] = (time.monotonic(), await job_state(print_job))
    finally:
        _refreshing.discard(print_job)


def cached_job_state(print_job: str | None, printed_at: datetime | None) -> str | None:
    """Job state for the chat page, without waiting for the printer: the last answer, asked again in the background.

    None when there is no job or it was printed more than a day ago. The first render shows "checking".
    """
    if not print_job or printed_at is None:
        return None
    if printed_at.tzinfo is None:
        printed_at = printed_at.replace(tzinfo=timezone.utc)
    if (utcnow() - printed_at).total_seconds() > RECENT:
        return None
    hit = _job_states.get(print_job)
    if (hit is None or time.monotonic() - hit[0] > JOB_TTL_S) and print_job not in _refreshing:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:                                  # no event loop (scripts, tests): nothing to ask
            return job_label(hit[1]) if hit else t("checking")
        _refreshing.add(print_job)
        task = loop.create_task(_refresh(print_job))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    return job_label(hit[1]) if hit else t("checking")
