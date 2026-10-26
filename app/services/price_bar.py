"""Drawing data of the price bar (_price_bar.html) from a deals.position() result. Pure: no request, no setting.

Both bases draw the user's zones on a 0..axis % scale of a reference price with a marker at 100 %: the Geizhals new
price ("New"), or – a bar of its own look, striped, its marker dashed – the median of the similar ads ("Ø"), its
limits named in euros. The usual range of the comparable ads (p25..p75) is a bracket under the track, clipped to the
bar. Labels are laid out for the narrowest
bar (NARROW_PX); a label that would touch another there shows only on bars of MID_PX or WIDE_PX and more (a
container query) when it fits there, else it is dropped: the caption and the legend name it anyway. The new-price
marker is always named: above the bar when that keeps clear of the price, else first in the lane below it.
"""
from __future__ import annotations

import math
from typing import Any

from app.i18n import t
from app.services import deals

NARROW_PX = 210     # narrowest bar: /deals/ad at a 768 px window (300 px column - 88 px padding); viewer at 768 px ~247
MID_PX = 260        # phones: the viewer at 360 px is ~272
WIDE_PX = 400       # the drawer and the desktop viewer, ~420
TIERS = (NARROW_PX, MID_PX, WIDE_PX)
TOP_PX, LANE_PX, GAP_PX = 7.4, 6.8, 6     # px per glyph: 12.5 px bold top lane / 11.5 px lane; free space between labels


def _span(x: float, text: str, px: float, bar_px: float, anchor: str | None = None) -> tuple[str, float, float]:
    """(anchor, left, right) in % of a bar of bar_px: centred on x, aligned to the edge where it would stick out
    (or the given anchor)."""
    w = len(text) * px / bar_px * 100
    anchor = anchor or ("l" if x - w / 2 < 0 else "r" if x + w / 2 > 100 else "c")
    return anchor, *{"l": (x, x + w), "r": (x - w, x), "c": (x - w / 2, x + w / 2)}[anchor]


def _row(labels: list[tuple[str, float, str]], px: float, tiers: tuple[int, ...] = TIERS) -> list[dict[str, Any]]:
    """One row of labels (kind, x, text) in order of importance: those that keep GAP_PX clear of the ones before
    on the narrowest bar, then per wider tier the rest that fit between them there ("from_px": the bar width from
    which it shows; None = always)."""
    out: dict[int, dict[str, Any]] = {}
    for bar_px in tiers:
        gap = GAP_PX / bar_px * 100
        taken = [_span(lb["x"], lb["text"], px, bar_px, lb["anchor"])[1:] for lb in out.values()]
        for i, (kind, x, text) in enumerate(labels):
            anchor, a, b = _span(x, text, px, bar_px)
            if i not in out and all(b + gap <= lo or a >= hi + gap for lo, hi in taken):
                taken.append((a, b))
                out[i] = {"kind": kind, "x": round(x, 1), "anchor": anchor, "text": text,
                          "from_px": None if bar_px == tiers[0] else bar_px}
    return [out[i] for i in sorted(out)]


def _eur(v: float) -> str:
    """Whole euros, half up (as the caption's cents would round): "€ 1.235"."""
    return "€ " + f"{math.floor(v + 0.5):,}".replace(",", ".")


def layout(p: dict[str, Any] | None, price_text: str | None = None) -> dict[str, Any] | None:
    """Drawing data of the price bar, x in % of the bar; None without a position. `price_text` is the label over
    the ring (the full bar); without it only the track is drawn (mini bars, the buy dialog)."""
    if not p or p.get("pos") is None:
        return None
    level = p.get("level")
    similar = p.get("basis") == "comparables"
    ref = p.get("median") if similar else p.get("new_price")
    if not (p.get("zones") and p.get("axis") and ref):
        return None                       # a stored result from before the zones (it is rated anew: rerate)
    ring = round(p["pos"] * 100, 1)
    band = None
    axis = p["axis"]
    edges = [0.0, *(min(100.0, b * 100 / axis) for b in p["zones"]), 100.0]
    new_at = round(100 * 100 / axis, 1)
    if p.get("band"):
        low, high = p["band"]["low"] * 100 / ref, p["band"]["high"] * 100 / ref     # % of the reference
        band = {"from": round(min(99.0, max(0.0, low * 100 / axis)), 1),
                "to": round(min(100.0, high * 100 / axis), 1),
                "off": high > axis, "out": low >= axis,          # off: reaches past the scale; out: all of it
                "text": f"{_eur(p['band']['low'])}–{_eur(p['band']['high'])}"}
    cut = [round(e, 1) for e in edges]
    segments = [{"level": n, "from": a, "to": b} for n, a, b in zip(deals.LEVEL_NAMES, cut, cut[1:], strict=False)
                if b > a]

    shown = price_text
    if price_text and p["pos"] >= 1 and (p.get("pct") or 0) >= p["axis"]:
        shown = price_text + " ›"                                        # past the end of the scale
    top = [("price", ring, shown)] if shown else []
    lane: list[tuple[str, float, str]] = []
    mark = "Ø" if similar else t("New")
    if new_at is not None:
        # the marker's name over it where it keeps clear of the price on every bar; else it heads the lane below, so
        # a price label that reaches over the marker never reads as the reference price
        with_new = _row([*top, ("new", new_at, mark)], TOP_PX, (NARROW_PX,))
        if any(lb["kind"] == "new" for lb in with_new):
            top.append(("new", new_at, mark))
        else:
            lane.append(("new", new_at, mark))
    z, axis = p["zones"], p["axis"]
    i = deals.LEVEL_NAMES.index(level)
    own = [k for k in (i - 1, i) if 0 <= k < 4]                    # the cuts bounding the current zone first
    rest = sorted((k for k in range(4) if k not in own), key=lambda k: abs(z[k] * 100 / axis - ring))
    # the similar ads' limits in euros (a share of a median says little), the new price's in % as set
    lane += [("cut", z[k] * 100 / axis, _eur(ref * z[k] / 100) if similar else f"{z[k]} %")
             for k in own + rest if z[k] < axis]

    if price_text and similar:
        aria = t("{price}: {level}, {n} % of the median of {count} similar ads", price=price_text,
                 level=deals.level_label(level), n=p.get("pct"), count=p.get("count") or 0)
    elif price_text:
        aria = t("{price}: {level}, {n} % of the new price", price=price_text, level=deals.level_label(level),
                 n=p.get("pct"))
    else:
        aria = deals.level_label(level)
    return {"basis": "comparables" if similar else "geizhals", "level": level, "segments": segments, "ring": ring,
            "new_at": new_at, "band": band, "top": _row(top, TOP_PX), "lane": _row(lane, LANE_PX), "aria": aria}
