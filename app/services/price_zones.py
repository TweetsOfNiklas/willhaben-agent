"""Price zones of the price check (Settings → Search & prices → Price rating): from which share of the lowest Geizhals new
price an ad counts as bargain, cheap, fair, expensive or very expensive, separately for used and new items – and, for
an ad rated by similar ads instead (no new price, or one that is not the item), from which share of their median.

One owner of reading, writing and checking the setting (the human_pace pattern). The arithmetic is in whole cents,
as search_agents._geizhals_cents: the floored % shown next to a label and the label itself never disagree.
"""
from __future__ import annotations

import json
import re
from typing import Any

from app.db import get_setting, set_setting
from app.i18n import t

KEY = "price_zones"     # JSON {"used": [4 ints], "new": [4 ints]}, only groups that differ from DEFAULTS; "" = defaults
NAMES = ("bargain", "cheap", "fair", "expensive", "very_expensive")
GROUPS = ("used", "new", "similar")      # similar: % of the median of the similar ads (deals.position, basis comparables)
# the "from" limits in % of the new price: from X % on the next label applies. Used: 55-74 % is the usual resale price
# of a used item in good condition; from 90 % on, new costs barely more and comes with warranty and return rights.
# New: a private sale has no return right, so fair is a bit below the lowest shop price; at that price, order it
# Similar ads: around their median is fair (90-114 %), a third below it a bargain, a third above it very expensive
DEFAULTS: dict[str, tuple[int, int, int, int]] = {"used": (35, 55, 75, 90), "new": (70, 85, 100, 115),
                                                  "similar": (70, 90, 115, 135)}
LOW, HIGH = 1, 300


class ZoneError(ValueError):
    """A group's limits are not 4 whole numbers LOW..HIGH (kind "range") or not strictly ascending ("order").
    `code` ("used-order") is what a redirect may carry; the text comes from error_text(), never from the request."""

    def __init__(self, group: str, kind: str):
        self.group, self.kind, self.code = group, kind, f"{group}-{kind}"
        super().__init__(error_text(self.code) or self.code)


def group(condition: str | None) -> str:
    """The zones an item condition uses: "new" only for new items; like new, refurbished, used and unknown are used."""
    return "new" if condition == "new" else "used"


def parse(values: Any, grp: str) -> tuple[int, int, int, int]:
    """Four whole numbers LOW..HIGH, each higher than the one before (" 30 " is fine; "30.5", "1e2", "1_0" and "+50"
    are not: the settings page's script accepts the same text)."""
    if not isinstance(values, (list, tuple)):
        raise ZoneError(grp, "range")
    texts = [str(x).strip() for x in values]
    if not all(re.fullmatch(r"[0-9]{1,3}", x) for x in texts):
        raise ZoneError(grp, "range")
    v = tuple(int(x) for x in texts)
    if len(v) != 4 or any(not LOW <= x <= HIGH for x in v):
        raise ZoneError(grp, "range")
    if any(b <= a for a, b in zip(v, v[1:], strict=False)):
        raise ZoneError(grp, "order")
    return v  # type: ignore[return-value]


def config() -> dict[str, tuple[int, ...]]:
    """The zones of both groups; a broken or missing group falls back to its defaults. Never raises. Read on every
    call (one settings row): a change applies at once everywhere."""
    try:
        raw = json.loads(get_setting(KEY) or "{}")
    except ValueError:
        raw = {}
    raw = raw if isinstance(raw, dict) else {}
    out: dict[str, tuple[int, ...]] = {}
    for g in GROUPS:
        try:
            out[g] = parse(raw[g], g) if g in raw else DEFAULTS[g]
        except ZoneError:
            out[g] = DEFAULTS[g]
    return out


def save(zones: dict[str, Any]) -> dict[str, tuple[int, ...]]:
    """Checks both groups first (all or nothing, ZoneError), then stores only the groups that differ from the
    defaults ("" when none does)."""
    current = config()
    parsed = {g: parse(zones.get(g), g) if zones.get(g) else current[g] for g in GROUPS}   # a form without a group keeps it
    custom = {g: list(v) for g, v in parsed.items() if v != DEFAULTS[g]}
    set_setting(KEY, json.dumps(custom) if custom else "")
    return parsed


def reset() -> dict[str, tuple[int, ...]]:
    """Both groups back to the defaults (the page's "Reset to default values")."""
    set_setting(KEY, "")
    return dict(DEFAULTS)


def _cents(v: float) -> int:
    return round(v * 100)


def share(price: float, new_price: float) -> int:
    """The price in whole % of the new price, rounded down (68.7 % is 68): what the UI shows."""
    return _cents(price) * 100 // _cents(new_price)


def share_exact(price: float, new_price: float) -> float:
    return _cents(price) * 100 / _cents(new_price)


def level_for(price: float, new_price: float, limits: tuple[int, ...] | list[int]) -> str:
    """The label of a price against "from" limits in % of the new price, compared in cents (no float edge)."""
    p, n = _cents(price), _cents(new_price)
    return NAMES[sum(p * 100 >= n * b for b in limits)]


def price_from(new_price: float, limit: int) -> float:
    """The cheapest whole-cent price that is at least `limit` % of the new price (where that label starts)."""
    n = _cents(new_price)
    return -(-n * limit // 100) / 100


def error_text(code: str | None) -> str | None:
    """The message of an error code from our own redirect ("used-range", "new-order"); None for anything else."""
    grp, _, kind = (code or "").partition("-")
    name = {"used": t("Used items"), "new": t("New items"), "similar": t("Similar ads")}.get(grp)
    if name is None:
        return None
    if kind == "range":
        return t("{group}: enter whole numbers from 1 to 300.", group=name)
    if kind == "order":
        return t("{group}: each limit must be higher than the one before.", group=name)
    return None
