"""Selling preferences (Settings → Selling preferences): handover, who pays the shipping, preferred carrier and
delivery, payment ways and free notes. Claude gets them as German lines in every answer to a buyer of my ads and in
every ad draft (`prompt_block`); the shipping card and the publish form start from them.

The free-text rules (Settings → My rules) still apply on top: the block says that they, the ad's own text and the
owner's notes/instruction for that ad win where they disagree, so an install that never opens this page (default: pickup
only) keeps answering as its rules and ads say.

Fixed fact, not a setting: willhaben censors e-mail addresses and links in chat messages and ads. Every reply and ad
prompt says so (`CONTACT_RULE`), and `contact_hits` checks our own texts in code (automatic answers, publishing, the
composer). Phone numbers are not treated as censored (the owner did not say so).
"""
from __future__ import annotations

import re
from typing import Any

from app.db import get_setting, set_setting
from app.i18n import t
from app.llm import prompts
from app.services.scam import EMAIL, EMAIL_SPELLED
from app.services.shipping_tariffs import CARRIERS

CONTACT_RULE = prompts.CONTACT_RULE     # German, the last bullet of the reply and ad system prompts

HANDOVERS = ("pickup", "shipping", "both")
COSTS = ("buyer", "included")           # who pays the shipping
DELIVERIES = ("any", "shop", "home")    # shop = parcel shop / locker
PAYMENTS = ("transfer", "paylivery", "cash")
KEYS = ("sell_handover", "sell_shipping_cost", "sell_delivery", "sell_carrier", "sell_payment", "sell_notes")
MAX_NOTES = 2000


def _pick(value: Any, allowed: tuple[str, ...], default: str) -> str:
    v = str(value or "").strip()
    return v if v in allowed else default


def _ways(raw: Any) -> list[str]:
    items = raw.split(",") if isinstance(raw, str) else list(raw or [])
    return [p for p in PAYMENTS if p in {str(x).strip() for x in items}]


def config() -> dict[str, Any]:
    """The stored preferences; values outside the choices count as the defaults."""
    raw = {k: get_setting(k) for k in KEYS}
    return {"handover": _pick(raw["sell_handover"], HANDOVERS, "pickup"),
            "shipping_cost": _pick(raw["sell_shipping_cost"], COSTS, "buyer"),
            "delivery": _pick(raw["sell_delivery"], DELIVERIES, "any"),
            "carrier": raw["sell_carrier"] if raw["sell_carrier"] in CARRIERS else "",
            "payment": _ways(raw["sell_payment"]),
            "notes": raw["sell_notes"].strip()}


def save(form: Any) -> None:
    """The settings form (POST /settings, section=selling). A value outside the choices keeps the stored one."""
    cur = config()
    set_setting("sell_handover", _pick(form.get("sell_handover"), HANDOVERS, cur["handover"]))
    set_setting("sell_shipping_cost", _pick(form.get("sell_shipping_cost"), COSTS, cur["shipping_cost"]))
    set_setting("sell_delivery", _pick(form.get("sell_delivery"), DELIVERIES, cur["delivery"]))
    set_setting("sell_carrier", _pick(form.get("sell_carrier"), ("", *CARRIERS), cur["carrier"]))
    ways = form.getlist("sell_payment") if hasattr(form, "getlist") else form.get("sell_payment")
    set_setting("sell_payment", ",".join(_ways(ways)))      # none ticked: no payment line for Claude
    if "sell_notes" in form:          # edited under Settings → AI rules (Selling) now; a form without it keeps it
        set_setting("sell_notes", str(form.get("sell_notes") or "").replace("\r\n", "\n").strip()[:MAX_NOTES])


def handover() -> str:
    """The owner's usual handover: preset of the publish form and the fallback when a publish names none."""
    return _pick(get_setting("sell_handover"), HANDOVERS, "pickup")


def shipping_preference() -> tuple[str, str]:
    """(carrier key or "", "any" | "shop" | "home") for ordering the shipping options."""
    return (c if (c := get_setting("sell_carrier")) in CARRIERS else "",
            _pick(get_setting("sell_delivery"), DELIVERIES, "any"))


def preference_text() -> str:
    """"GLS, parcel shop" for the shipping card; "" without a preference."""
    carrier, delivery = shipping_preference()
    parts = [CARRIERS[carrier].name] if carrier else []
    if delivery == "shop":
        parts.append(t("parcel shop"))
    elif delivery == "home":
        parts.append(t("door"))
    return ", ".join(parts)


# ------------------------------------------------------------------ the lines Claude gets (German on purpose)
_SHOP = "Zustellung an einen Paketshop/eine Paketbox"
_HOME = "Zustellung an die Haustür"
_PAY = {"transfer": "Überweisung", "paylivery": "willhaben PayLivery", "cash": "bar bei Abholung"}


def prompt_block(*, for_ad: bool = False) -> str:
    """German lines for the reply and ad prompts. Only what fits the handover: no shipping lines with pickup only,
    no PayLivery without shipping (it is willhaben's shipping service), no cash without pickup."""
    c = config()
    pickup, ships = c["handover"] in ("pickup", "both"), c["handover"] in ("shipping", "both")
    lines = [{"pickup": "Übergabe: nur Abholung, kein Versand", "shipping": "Übergabe: nur Versand, keine Abholung",
              "both": "Übergabe: Abholung oder Versand"}[c["handover"]]]
    if ships:
        if c["shipping_cost"] == "included":
            lines.append({"shop": f"Versandkosten: im Preis inkludiert bei {_SHOP}; {_HOME} gegen Aufpreis",
                          "home": f"Versandkosten: im Preis inkludiert ({_HOME})",
                          "any": "Versandkosten: im Preis inkludiert"}[c["delivery"]])
        else:
            lines.append("Versandkosten: zahlt der Käufer zusätzlich"
                         + {"shop": f" (bevorzugt {_SHOP})", "home": f" ({_HOME})", "any": ""}[c["delivery"]])
        if c["carrier"]:
            lines.append(f"Bevorzugter Paketdienst: {CARRIERS[c['carrier']].name}")
    pay = [p for p in c["payment"] if (p != "paylivery" or ships) and (p != "cash" or pickup)]
    if pay:
        lines.append("Zahlung: " + ", ".join(_PAY[p] for p in pay))
    if ships and c["shipping_cost"] == "included" and "paylivery" in pay:
        lines.append("Bei PayLivery zahlt der Käufer den Versand über willhaben; „inklusive“ gilt nur bei Überweisung.")
    out = ["Meine Verkaufsvorgaben (allgemein; sagen meine Regeln oben, die Anzeige selbst oder meine Angaben zu dieser "
           "Anzeige etwas anderes, gilt das):",
           *(f"- {x}" for x in lines)]
    if c["notes"]:
        out += ["Weitere Angaben zum Verkauf:", c["notes"]]
    if for_ad:
        out.append("Erwähne die Übergabe/Versand-Regel am Ende der Beschreibung in einem Satz.")
    return "\n".join(out)


def append_to(rules: str, *, for_ad: bool = False) -> str:
    """The free-text rules followed by the preferences block (what the prompts get as "my rules")."""
    block = prompt_block(for_ad=for_ad)
    return f"{rules.strip()}\n\n{block}" if (rules or "").strip() else block


# ------------------------------------------------------------------ e-mail addresses and links in our own texts
_STOP = r"""[^\s<>"'„“”»«()\[\]]"""
_URL = re.compile(rf"\bhttps?://{_STOP}+|\bwww\.{_STOP}+", re.I)
# a bare domain only with a common top-level domain: "s.Oliver", "B.Braun", "z.B." or "Ja.gerne" are no links
_TLDS = ("at", "de", "ch", "li", "eu", "com", "net", "org", "info", "biz", "io", "co", "me", "ly", "gl", "to", "tv",
         "app", "dev", "shop", "store", "online", "site", "link", "xyz", "it", "fr", "nl", "uk", "us", "pl", "cz", "sk",
         "hu", "si", "hr")
_HOST = re.compile(rf"(?<![\w@./:-])(?:[^\W_](?:[\w-]*[^\W_])?\.)+(?:{'|'.join(_TLDS)})(?:/{_STOP}*)?(?![\w@-])",
                   re.I)


def contact_hits(text: str | None) -> list[str]:
    """E-mail addresses (also spelled "name (at) host (punkt) at") and links in a text, in order, each once.
    willhaben censors them, so our own answers and ads must not contain any. Prices ("4,50"), article numbers
    ("06019H5202") and abbreviations ("z.B.", "ca.") are no hits."""
    text = text or ""
    hits: list[tuple[int, int, str]] = []
    for rx in (EMAIL, EMAIL_SPELLED, _URL, _HOST):
        for m in rx.finditer(text):
            a, b = m.span()
            if any(a < e and s < b for s, e, _ in hits):      # part of an earlier hit (the host of an address)
                continue
            hits.append((a, b, m.group(0).rstrip(".,;:!?")))
    out: list[str] = []
    for _, _, h in sorted(hits):
        if h not in out:
            out.append(h)
    return out
