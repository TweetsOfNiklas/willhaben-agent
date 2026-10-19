"""Shipping to the owner, as the sites state it: a price (0 = free, None = not stated) and whether the seller ships at
all ("ship", "pickup" = pickup only, None = not stated). One place for copying it between listings, ad pages and
hits, for the label every price shows it with ("+ € 5,00 shipping", "free shipping", "pickup only") and for the
line Claude's prompts get.

Where it comes from: eBay's result list (calculated by eBay for the owner's postcode, Settings → Shipping address)
and its item page, Preisjäger's deal, willhaben's PayLivery price, Kleinanzeigen's shipping option and Vinted's
cheapest option on the ad page, Shpock's "shippable" and Facebook's delivery types in the list. An ad page is loaded
for that only for hits that pass an agent's checks (before their push), when the ad is opened and on "Sync".
"""
from __future__ import annotations

import re
from typing import Any

from app.i18n import t

SHIP, PICKUP = "ship", "pickup"
_AMOUNT = re.compile(r"(\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?|\d+(?:[.,]\d{1,2})?)")


def from_text(text: str | None) -> tuple[float | None, str | None]:
    """A site's shipping words -> (price, kind): "+EUR 5,00 Versand" (5.0, ship), "Kostenloser Versand" /
    "Gratisversand" (0.0, ship), "Nur Abholung" (None, pickup); (None, None) for anything else."""
    s = (text or "").strip()
    low = s.lower()
    if not low:
        return None, None
    if "nur abholung" in low or "only pickup" in low or "local pickup only" in low:
        return None, PICKUP
    if any(w in low for w in ("kostenlos", "gratis", "free shipping", "versandkostenfrei")):
        return 0.0, SHIP
    if "versand" in low or "shipping" in low or "lieferung" in low:
        m = _AMOUNT.search(low)
        if m:
            raw = m.group(1)
            value = float(raw.replace(".", "").replace(",", ".")) if "," in raw else float(raw)
            return value, SHIP
        return None, SHIP
    return None, None


def apply_shipping(target: Any, source: Any) -> None:
    """Copy what `source` states onto `target` (hit, listing, ad page); what it does not state stays as it was: a
    result list without shipping keeps what the ad page told."""
    price, kind = getattr(source, "shipping_price", None), getattr(source, "shipping", None)
    if price is not None:
        target.shipping_price = price
        kind = kind or SHIP
    if kind:
        target.shipping = kind
        if kind == PICKUP:
            target.shipping_price = None
    if (fee := getattr(source, "buyer_fee", None)) is not None and hasattr(target, "buyer_fee"):
        target.buyer_fee = fee                      # the site's own buyer fee (app/platforms/fees.py)


def label(price: float | None, kind: str | None, currency: str = "€") -> str:
    """The words next to a price; "" when nothing is known."""
    if kind == PICKUP:
        return t("pickup only")
    if price is not None:
        if price <= 0:
            return t("free shipping")
        amount = f"{currency} {price:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        return t("+ {amount} shipping", amount=amount)
    if kind == SHIP:
        return t("shipping possible")
    return ""


def label_of(x: Any, currency: str = "€") -> str:
    """label() of a hit, listing, ad page or a stored result dict (old ones have no shipping keys), with the buyer fee
    the site states for the ad ("+ € 4,90 shipping · + € 1,15 buyer protection")."""
    get = x.get if isinstance(x, dict) else (lambda k: getattr(x, k, None))
    text = label(get("shipping_price"), get("shipping"), currency)
    fee = get("buyer_fee")
    if fee:
        amount = f"{currency} {fee:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        text = " · ".join(x for x in (text, t("+ {amount} buyer protection", amount=amount)) if x)
    return text


def total(price: float | None, shipping_price: float | None) -> float | None:
    """Price including shipping, when both are known."""
    return None if price is None or shipping_price is None else round(price + shipping_price, 2)


def with_price(price_text: str | None, price: float | None, kind: str | None) -> str | None:
    """A price for a prompt with its shipping, when the site states any: "120 € (Versand: 5 €)"."""
    if price is None and not kind:
        return price_text
    return f"{price_text or 'kein Preis'} ({prompt_line(price, kind).split(' (')[0]})"


def prompt_line(price: float | None, kind: str | None) -> str:
    """For Claude's prompts (German, as the rest of them): what shipping costs, or that it is not stated."""
    if kind == PICKUP:
        return "Versand: nur Abholung"
    if price is not None:
        return "Versand: kostenlos" if price <= 0 else f"Versand: {price:g} € (Gesamtpreis mit Versand höher)"
    if kind == SHIP:
        return "Versand: möglich (die Kosten lädt die App selbst, kein Bedenken)"
    return "Versand: nicht angegeben"
