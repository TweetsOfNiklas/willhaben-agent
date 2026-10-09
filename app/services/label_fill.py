"""Fill a carrier's online label form in the remote browser (the user checks and pays there).

Carrier forms change often, so nothing is hard-coded per field: on every step the visible form fields
(label, name, placeholder, autocomplete, options) are collected from the page, Claude maps them to the
shipment data, and Playwright fills them. Without Claude a keyword heuristic fills the obvious fields.

Safety: this only types into fields and ticks radio buttons/checkboxes. It never clicks buttons or links,
so it can never send an order or pay; the user clicks "Weiter"/"Bezahlen" in the remote view.
Downloads in that tab (the label PDF) are saved to DATA_DIR/labels and attached to the shipment.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from app.config import get_settings
from app.i18n import t
from app.services import shipping
from app.services.shipping_tariffs import CARRIERS

log = logging.getLogger(__name__)

MAX_FIELDS = 80
_active: dict[str, Any] = {"shipment": None, "page": None}

# Collect visible, enabled form controls of one frame; tags them with data-whfill="<n>".
_COLLECT_JS = r"""(start) => {
  const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return (r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none') || el.type === 'radio' || el.type === 'checkbox'; };
  const txt = s => (s || '').replace(/\s+/g, ' ').trim().slice(0, 120);
  const labelOf = el => {
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) return txt(l.innerText); }
    const p = el.closest('label'); if (p) return txt(p.innerText);
    const by = el.getAttribute('aria-labelledby');
    if (by) return txt(by.split(' ').map(i => (document.getElementById(i) || {}).innerText || '').join(' '));
    let prev = el.previousElementSibling; if (prev && prev.innerText) return txt(prev.innerText);
    const box = el.parentElement && el.parentElement.parentElement; return box ? txt(box.innerText).slice(0, 60) : '';
  };
  const out = []; let n = start;
  for (const el of document.querySelectorAll('input, select, textarea')) {
    const type = (el.type || el.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'file', 'reset', 'password'].includes(type) || el.disabled || el.readOnly || !vis(el)) continue;
    el.setAttribute('data-whfill', String(n));
    const f = {idx: n++, tag: el.tagName.toLowerCase(), type, name: el.name || '', id: el.id || '',
               label: labelOf(el), placeholder: el.placeholder || '', autocomplete: el.autocomplete || '',
               aria: el.getAttribute('aria-label') || '', value: type === 'checkbox' || type === 'radio' ? (el.checked ? 'checked' : '') : (el.value || '')};
    if (el.tagName === 'SELECT') f.options = [...el.options].slice(0, 40).map(o => txt(o.text) + ' = ' + o.value);
    out.push(f);
  }
  return out;
}"""

FILL_SYSTEM = """\
Du füllst ein Online-Formular eines Paketdienstes (Paketschein für Privatkunden in Österreich) aus.
Du bekommst die sichtbaren Formularfelder (idx, Typ, Beschriftung, name, Optionen) und die Sendungsdaten.
- fills: für jedes Feld, das zu den Daten passt, {idx, value}. Absender-Felder bekommen die Absenderdaten,
  Empfänger-Felder die Empfängerdaten. Achte auf Abschnitte/Beschriftungen wie "Absender", "Empfänger", "Von", "An".
- Bei select: value = genau der Options-Wert (Teil nach " = "). Bei radio/checkbox: value = "check" zum Anhaken.
  Hake nur an, was eindeutig zur Sendung passt (Paketgröße, Zustellart Haustür/Paketshop, Abgabeart);
  niemals AGB, Datenschutz, Newsletter, Zusatzversicherung oder Zahlungsarten.
- Felder, die schon den richtigen Wert haben oder zu nichts passen, lässt du weg. Erfinde keine Daten.
- Länderfelder: Österreich. Name ggf. in Vor- und Nachname aufteilen (letztes Wort = Nachname).
- note: kurz, was noch fehlt oder was die Person selbst wählen muss, sonst "".
"""

FILL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "fills": {"type": "array", "items": {"type": "object", "properties": {
            "idx": {"type": "integer"}, "value": {"type": "string"}},
            "required": ["idx", "value"], "additionalProperties": False}},
        "note": {"type": "string"},
    },
    "required": ["fills", "note"],
    "additionalProperties": False,
}

_NEVER = re.compile(r"agb|datenschutz|newsletter|werbung|versicherung|insurance|terms|privacy|zahl|pay|kauf", re.I)


def _data_for(s) -> dict[str, Any]:
    o = shipping.by_key(s.option)
    p = s.parcel or {}
    return {
        "absender": shipping.sender(),
        "empfaenger": {**(s.recipient or {}), "country": "Österreich"},
        "paket": {"laenge_cm": p.get("length_cm"), "breite_cm": p.get("width_cm"), "hoehe_cm": p.get("height_cm"),
                  "gewicht_kg": p.get("weight_kg"), "inhalt": p.get("contents") or "Gebrauchtware",
                  "groesse": o.size if o else None, "zustellung": o.delivery if o else None,
                  "produkt": o.label() if o else None},
    }


def _prompt(data: dict[str, Any], fields: list[dict[str, Any]]) -> str:
    import json
    return "\n".join(["Sendungsdaten:", json.dumps(data, ensure_ascii=False, indent=1), "",
                      "Formularfelder (sichtbar auf der aktuellen Seite):",
                      json.dumps(fields, ensure_ascii=False), "", "Aufgabe: Welche Felder füllst du wie?"])


# ------------------------------------------------------------------ keyword fallback (no Claude)
_KEYS = [  # (regex on label/name/placeholder/autocomplete, data path)
    (r"vorname|first.?name|given", "first"), (r"nachname|last.?name|family|surname", "last"),
    (r"^(name|full.?name|vor- und nachname)$|\bname\b", "name"), (r"hausn|house|hnr|nummer\b", "house_no"),
    (r"stra(ss|ß)e|street|address.?line.?1|adresse", "street"), (r"stiege|t[üu]r|zusatz|address.?line.?2|extra", "extra"),
    (r"plz|postleitzahl|postal|zip", "zip"), (r"\bort\b|stadt|city|locality|address-level2", "city"),
    (r"telefon|phone|mobil|\btel\b", "phone"), (r"e-?mail", "email"),
]


def _heuristic(data: dict[str, Any], fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Text fields only; a field belongs to the recipient once a "Empfänger" section has started."""
    fills, part = [], "absender"
    for f in fields:
        hay = " ".join(str(f.get(k) or "") for k in ("label", "name", "id", "placeholder", "autocomplete", "aria")).lower()
        if re.search(r"empf[äa]nger|recipient|receiver|\ban\b|to\b", f.get("label", "").lower()):
            part = "empfaenger"
        if f["type"] in ("radio", "checkbox") or f["tag"] == "select" or f.get("value"):
            continue
        src = data[part]
        if "empf" in hay or "recipient" in hay:
            src = data["empfaenger"]
        elif "absend" in hay or "sender" in hay:
            src = data["absender"]
        for rx, key in _KEYS:
            if re.search(rx, hay):
                first, _, last = (src.get("name") or "").rpartition(" ")
                val = {"first": first or last, "last": last if first else ""}.get(key, src.get(key))
                if val:
                    fills.append({"idx": f["idx"], "value": str(val)})
                break
    return fills


# ------------------------------------------------------------------ browser
async def _collect(page) -> tuple[list[dict[str, Any]], dict[int, Any]]:
    fields, where = [], {}
    for frame in page.frames:
        if frame != page.main_frame and frame.url.split("/")[2:3] != page.url.split("/")[2:3]:
            continue                      # foreign iframes (payment providers) stay untouched
        try:
            got = await frame.evaluate(_COLLECT_JS, len(fields) + 1)
        except Exception as e:  # noqa: BLE001 - detached/cross-origin frame
            log.debug("collect failed in frame %s: %s", frame.url, e)
            continue
        for f in got:
            where[f["idx"]] = frame
        fields += got
    return fields[:MAX_FIELDS], where


async def _apply(fields: list[dict[str, Any]], where: dict[int, Any], fills: list[dict[str, Any]]) -> int:
    by_idx = {f["idx"]: f for f in fields}
    done = 0
    for item in fills:
        f, frame = by_idx.get(item.get("idx")), where.get(item.get("idx"))
        value = str(item.get("value") or "")
        if f is None or frame is None or not value:
            continue
        loc = frame.locator(f'[data-whfill="{f["idx"]}"]')
        try:
            if f["type"] in ("radio", "checkbox"):
                if _NEVER.search(f.get("label", "") + " " + f.get("name", "")):
                    continue              # never tick terms, insurance, payment
                await loc.check(timeout=3000, force=True)
            elif f["tag"] == "select":
                await loc.select_option(value=value, timeout=3000)
            else:
                await loc.fill(value, timeout=3000)
                await loc.dispatch_event("blur")
            done += 1
        except Exception as e:  # noqa: BLE001 - one stubborn field must not stop the rest
            log.info("fill %s failed: %s", f.get("name") or f.get("label"), e)
    return done


def _on_download(shipment_id: int):
    async def save(download) -> None:
        d = get_settings().data_dir / "labels"
        d.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^\w.-]", "_", download.suggested_filename or "label.pdf")
        path = d / f"shipment-{shipment_id}-{name}"
        await download.save_as(path)
        shipping.update(shipment_id, label_path=path.name, status="labelled")
        log.info("label saved for shipment %s: %s", shipment_id, path.name)
    return save


async def start(shipment_id: int) -> str:
    """Open the carrier's label page for the shipment's option and fill the first step."""
    from app.willhaben.session import get_session

    s = shipping.get(shipment_id)
    if s is None or not s.option or not s.recipient:
        raise shipping.ShippingError(t("Address and parcel option needed first"))
    if not shipping.sender_complete():
        raise shipping.ShippingError(t("Enter your sender address in Settings → Shipping first"))
    o = shipping.by_key(s.option)
    if o is None or not o.online:
        raise shipping.ShippingError(t("This option has no online label – buy it at the counter"))
    sess = get_session()
    if not sess.started:
        raise shipping.ShippingError(t("Browser session is not running"))
    page = await sess.context.new_page()            # becomes the visible page of the remote view
    page.on("download", lambda dl: _spawn(_on_download(shipment_id)(dl)))
    await page.goto(CARRIERS[o.carrier].label_url, wait_until="domcontentloaded")
    _active.update(shipment=shipment_id, page=page)
    try:
        return await fill_current()
    except Exception as e:  # noqa: BLE001 - the page is open; the user can still fill by hand
        log.warning("first fill failed: %s", e)
        return t("Page opened – fill it with “Fill this page” once the form is visible")


async def fill_current() -> str:
    """Fill the form step that is visible now (button on the session page)."""
    from app.llm.base import LLMError, get_backend

    page, sid = _active.get("page"), _active.get("shipment")
    if page is None or page.is_closed() or sid is None:
        raise shipping.ShippingError(t("No label form open"))
    s = shipping.get(sid)
    await page.wait_for_load_state("domcontentloaded")
    fields, where = await _collect(page)
    if not fields:
        return t("No form fields on this page – log in or continue, then press “Fill this page” again")
    data = _data_for(s)
    note = ""
    try:
        res = await get_backend("chat").complete(system=FILL_SYSTEM, schema=FILL_SCHEMA, prompt=_prompt(data, fields))
        fills, note = res.data.get("fills") or [], (res.data.get("note") or "").strip()
    except LLMError as e:
        log.info("Claude mapping failed, using keyword fill: %s", e)
        fills = _heuristic(data, fields)
    n = await _apply(fields, where, fills)
    msg = t("{n} fields filled – check them, then continue on the carrier's page", n=n)
    return f"{msg}\n{note}" if note else msg


def active() -> dict[str, Any] | None:
    page, sid = _active.get("page"), _active.get("shipment")
    if page is None or sid is None or page.is_closed():
        return None
    return {"shipment_id": sid, "url": page.url}


_bg: set = set()


def _spawn(coro) -> None:
    import asyncio
    task = asyncio.ensure_future(coro)
    _bg.add(task)
    task.add_done_callback(_bg.discard)
