"""Buyer fees of the sites' payment services: what a buyer pays on top of item and shipping when paying through the
site (willhaben PayLivery, Vinted's buyer protection, Kleinanzeigen "Sicher bezahlen"). The seller pays nothing on
any of them; pickup and payment outside the site have no fee. eBay (private sellers in Germany), Shpock, Facebook and
Preisjäger charge the buyer nothing.

A rule is a percentage of the item price plus a fixed part, clamped to a minimum and maximum, in whole cents (rounded
half up; the buy dialog mirrors the maths in wbBuy.feeCents). `rule(platform, category)` picks the rule of an ad:
a category rule (the category path from leaf to root, by name or id, CATEGORY_RULES) before the site's own one
(RULES). A fee the site states for the very ad (Vinted's list sends `serviceFee`) always beats the estimate.

Sources (checked 2026-10-19):
- willhaben PayLivery: help centre "Wie viel kostet PayLivery" (updated 2026-10-09): 4.5 %, at least € 0.99, at most
  € 49.99, "the fee can vary by category and item price". No category rates are published; willhaben shows the
  exact fee in the PayLivery window of the chat.
- Vinted: 5 % + € 0.70 (Vinted's catalog: € 2.00 -> € 0.80, € 8.99 -> € 1.15, € 20.00 -> € 1.70).
- Kleinanzeigen "Sicher bezahlen": 4.5 % + € 0.50 (2026 guides; older ones say € 0.35).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class FeeRule:
    service: str               # the fee's name in Claude's German prompts ("willhaben Käuferschutzgebühr")
    pct: float                 # percent of the item price, one decimal
    fixed: float = 0.0         # euro per purchase
    min: float = 0.0
    max: float | None = None
    category: str | None = None   # the category the rule is for (None: the site's general rule)

    def cents(self, item_cents: int) -> int:
        """The fee in cents for an item price in cents; 0 without a positive price."""
        if item_cents <= 0:
            return 0
        raw = (item_cents * round(self.pct * 10) + 500) // 1000 + _cents(self.fixed)
        raw = max(raw, _cents(self.min))
        return min(raw, _cents(self.max)) if self.max is not None else raw

    def fee(self, item_price: float | None) -> float:
        if item_price is None or item_price <= 0:
            return 0.0
        return self.cents(_cents(item_price)) / 100

    def item_for_cents(self, avail: int) -> int:
        """The largest item price (cents) whose price plus fee is at most `avail` cents (item + fee grows with the
        item price, so a binary search finds it; the buy dialog does the same)."""
        lo, hi = 0, max(avail, 0)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if mid + self.cents(mid) <= avail:
                lo = mid
            else:
                hi = mid - 1
        return lo

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _cents(v: float | None) -> int:
    return int(round(float(v or 0) * 100))


RULES: dict[str, FeeRule] = {
    "willhaben": FeeRule("willhaben Käuferschutzgebühr", 4.5, 0.0, 0.99, 49.99),
    "vinted": FeeRule("Vinted Käuferschutzgebühr", 5.0, 0.70),
    "kleinanzeigen": FeeRule("Kleinanzeigen-Gebühr für „Sicher bezahlen“", 4.5, 0.50),
}

# Category rates that differ from the site's rule: {platform: {category name or id (lower case): FeeRule}}. Empty
# until a site publishes them or they are read from its checkout.
CATEGORY_RULES: dict[str, dict[str, FeeRule]] = {}


def rule(platform: str | None, category: Iterable[str] | str | None = None) -> FeeRule | None:
    """The fee rule of an ad on this site: the most specific category rule (leaf first), else the site's own; None
    for sites without a buyer fee."""
    platform = platform or "willhaben"
    base = RULES.get(platform)
    if base is None:
        return None
    path = [category] if isinstance(category, str) else list(category or [])
    special = CATEGORY_RULES.get(platform) or {}
    for part in reversed(path):
        if (r := special.get(str(part).strip().lower())) is not None:
            return r
    return base


def from_dict(d: dict[str, Any] | None, platform: str | None = None) -> FeeRule | None:
    """A rule stored with an order (FeeRule.as_dict); the site's rule when nothing (or something broken) is stored."""
    if isinstance(d, dict):
        try:
            return FeeRule(service=str(d.get("service") or ""), pct=float(d["pct"]), fixed=float(d.get("fixed") or 0),
                           min=float(d.get("min") or 0), max=float(d["max"]) if d.get("max") is not None else None,
                           category=d.get("category"))
        except (KeyError, TypeError, ValueError):
            pass
    return rule(platform)


def fee(platform: str | None, item_price: float | None, category: Iterable[str] | str | None = None,
        known: float | None = None) -> float:
    """The buyer fee of an item: what the site stated for this ad (`known`), else the rule's estimate; 0 for sites
    without one."""
    if known is not None:
        return round(float(known), 2)
    r = rule(platform, category)
    return r.fee(item_price) if r else 0.0


def text_de(r: FeeRule) -> str:
    """The rule in Claude's German prompts: "Vinted Käuferschutzgebühr 5 % des Artikelpreises + 0,70 €, zahlt der
    Käufer"."""
    def money(v: float) -> str:
        return f"{v:.2f} €".replace(".", ",")
    parts = [f"{r.pct:g} % des Artikelpreises".replace(".", ",")]
    if r.fixed:
        parts[0] += f" + {money(r.fixed)}"
    if r.min:
        parts.append(f"mind. {money(r.min)}")
    if r.max is not None:
        parts.append(f"max. {money(r.max)}")
    return f"{r.service} {', '.join(parts)}, zahlt der Käufer"


def ad_fee(platform: str | None, price: float | None, category: Iterable[str] | str | None = None,
           known: float | None = None, paylivery: bool = False) -> dict[str, Any] | None:
    """The buyer fee line of an ad's price breakdown: {"amount", "service", "exact"} (exact: the site stated it),
    None when the buyer pays none for a shipped purchase. willhaben only with PayLivery; Kleinanzeigen' fee belongs to
    the optional "Sicher bezahlen" and is left out."""
    platform = platform or "willhaben"
    if not price or price <= 0 or platform not in ("willhaben", "vinted") or (platform == "willhaben" and not paylivery):
        return None
    r = rule(platform, category)
    if r is None:
        return None
    amount = fee(platform, price, category, known=known)
    return {"amount": amount, "service": r.service, "exact": known is not None} if amount > 0 else None
