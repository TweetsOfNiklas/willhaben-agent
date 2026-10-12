"""Minimum price per own ad: parsing the form value and the code-side guard for automatic answers.

The guard is deliberately independent of the model: whatever Claude says in `auto_send`, an automatic answer
that names or suggests a price below `MyAd.min_price` is held back as a draft (approval push).
False positives are fine (they only cost one tap on the push); false negatives are not.
Needs live verification: tested with fake replies only, not yet against real buyer price talks.
Not caught by the regex: amounts in words ("zwanzig") or without a currency sign ("ok, 15 passt") -
those rely on `suggested_price` and on the prompt.
"""
from __future__ import annotations

import re

from app.i18n import t

# 1.000 / 1'000 thousands, optional ,50 / .50 decimals
_NUM = r"\d+(?:[.'’]\d{3})*(?:[.,]\d{1,2})?"
_AMOUNT = re.compile(
    rf"(?:€|\bEUR\b|\bEuro\b)\s*(?P<a>{_NUM})(?!\d)"                  # € 25, €25, EUR 25, Euro 25
    rf"|(?<![\d.,])(?P<b>{_NUM})\s*(?:€|EUR\b|Euros?\b|Eur\b|,-|\.-)",   # 25 €, 25€, 25 Euro, 25,-, 25.-
    re.I,
)
# Shipping amounts are not the item price, but only in tight, unambiguous phrasings, so totals such as
# "10 € inkl. Versand" or "Versand ist dabei, 10 €" still count:
#   "Versand 5 €", "Versand kostet 4,50 €", "Porto: 5 €"      (shipping word, then only filler words)
#   "20 € + 5 € Versand", "20 € zzgl. 5 € Porto"              (plus-sign/zzgl. before, shipping word right after)
# but not when the shipping word is qualified ("inkl. Versand 15 €", "mit Versand wären 15 €", "Ohne Versand 15 €"):
# that is the total or item price. "Paket" is not a shipping word here - on willhaben it usually means a bundle.
_FILLER = r"(?:kostet|kosten|beträgt|betragen|wäre|wären|ist|sind|extra|zusätzlich|ca\.?|nochmal|innerhalb|österreich|nach)"
_SHIPPING_BEFORE = re.compile(rf"\b(?:versand(?:kosten)?|porto)\s*[:=]?\s*(?:{_FILLER}\s*[:=]?\s*){{0,3}}$", re.I)
_QUALIFIED = re.compile(r"\b(?:inkl\.?|inklusive|incl\.?|mit|ohne|samt|exkl\.?|exklusive)\s+(?:dem\s+|den\s+)?$", re.I)
_PLUS_BEFORE = re.compile(r"(?:\+|\bplus|\bzzgl\.?|\bzuzüglich)\s*$", re.I)
_SHIPPING_AFTER = re.compile(r"\s*(?:für\s+(?:den\s+|das\s+)?)?(?:versand|porto)", re.I)


def to_float(raw: str) -> float:
    """'25' / '25,50' / '1.000' / '1.000,50' / "1'000" -> float."""
    s = raw.replace("'", "").replace("’", "")
    if re.fullmatch(r"\d+(?:\.\d{3})+(?:,\d{1,2})?", s):     # 1.000 or 1.000,50
        s = s.replace(".", "")
    return float(s.replace(",", "."))


def parse_form(raw: str | None) -> float | None:
    """Form value of 'Untergrenze (€)': '' -> None (no floor). Raises ValueError for junk or negatives."""
    s = (raw or "").strip().replace("€", "").replace(",-", "").strip()
    if not s:
        return None
    if not re.fullmatch(_NUM, s):
        raise ValueError(s)
    v = to_float(s)
    if v < 0:
        raise ValueError(s)
    return v


def euro_amounts(text: str) -> list[float]:
    """Euro amounts named in a reply (item prices; shipping amounts right after a shipping word are skipped)."""
    out = []
    for m in _AMOUNT.finditer(text or ""):
        before, after = text[:m.start()], text[m.end():]
        ship = _SHIPPING_BEFORE.search(before)
        if ship and _QUALIFIED.search(before[:ship.start()]):
            ship = None                                   # "inkl./mit/ohne Versand 15 €" names the total / item price
        if ship or (_PLUS_BEFORE.search(before) and _SHIPPING_AFTER.match(after)):
            continue
        out.append(to_float(m.group("a") or m.group("b")))
    return out


def _below(floor: float, suggested_price: float | None, text: str) -> list[float]:
    """Amounts of an answer below a floor: the suggested price first, then the euro amounts of the text."""
    out = [suggested_price] if suggested_price is not None and suggested_price < floor - 0.005 else []
    return out + [a for a in euro_amounts(text) if a < floor - 0.005]


def asking_value(raw: float | str | None) -> float | None:
    """The asking price of the ad context: a number, or the first euro amount of the display text ("20 €", "VB 20 €");
    None for "Zu verschenken" or nothing."""
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    found = euro_amounts(str(raw or ""))
    if found:
        return found[0]
    s = str(raw or "").strip()
    return to_float(s) if re.fullmatch(_NUM, s) else None


def blockers(min_price: float | None, suggested_price: float | None, text: str, *,
             asking: float | None = None, intent: str | None = None) -> list[str]:
    """Reasons to hold an automatic answer back. Without a floor the asking price is the floor, so a buyer can't talk
    the agent into "ok, 1 € passt" (prompt injection included); without either, any price talk waits for the user."""
    if min_price is None:
        if asking is not None:
            return [t("No minimum price set: {amount} is below the asking price {asking}", amount=_fmt(a),
                      asking=_fmt(asking)) for a in _below(asking, suggested_price, text)[:1]]
        if suggested_price is not None or intent == "price_negotiation" or euro_amounts(text):
            return [t("No minimum price and no asking price known – price talks wait for you")]
        return []
    why = []
    if suggested_price is not None and suggested_price < min_price:
        why.append(t("Suggested price {price} is below the minimum {min}", price=_fmt(suggested_price), min=_fmt(min_price)))
    low = [a for a in euro_amounts(text) if a < min_price]
    if low:
        why.append(t("Answer names {amount}, below the minimum {min}", amount=_fmt(min(low)), min=_fmt(min_price)))
    return why


def _fmt(v: float) -> str:
    return f"{v:g} €" if v == int(v) else f"{v:.2f} €".replace(".", ",")
