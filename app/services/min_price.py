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
_FILLER = r"(?:kostet|kosten|beträgt|betragen|wäre|wären|ist|sind|extra|zusätzlich|ca\.?|nochmal|innerhalb|österreich|nach)"
_SHIPPING_BEFORE = re.compile(rf"\b(?:versand(?:kosten)?|porto|paket(?:preis)?)\s*[:=]?\s*(?:{_FILLER}\s*[:=]?\s*){{0,3}}$", re.I)
_PLUS_BEFORE = re.compile(r"(?:\+|\bplus|\bzzgl\.?|\bzuzüglich)\s*$", re.I)
_SHIPPING_AFTER = re.compile(r"\s*(?:für\s+(?:den\s+|das\s+)?)?(?:versand|porto|paket)", re.I)


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
        if _SHIPPING_BEFORE.search(before) or (_PLUS_BEFORE.search(before) and _SHIPPING_AFTER.match(after)):
            continue
        out.append(to_float(m.group("a") or m.group("b")))
    return out


def blockers(min_price: float | None, suggested_price: float | None, text: str) -> list[str]:
    """Reasons to hold an automatic answer back. No floor set -> nothing (the model's auto_send decides, as before)."""
    if min_price is None:
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
