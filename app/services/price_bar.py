"""Drawing data of the price bar (_price_bar.html) from a deals.position() result. Pure: no request, no setting.

Zones basis: the user's zones on a 0..axis % scale of the Geizhals new price, a marker at 100 % (new) and the usual
range of the comparable ads (p25..p75) as a strip under the track, clipped to the bar. Comparables basis: fixed
percentile segments (0/10/25/75/90/100), the fair one being the usual range. Labels are laid out for the narrowest
bar (NARROW_PX); a label that would touch another there is shown only on bars of WIDE_PX and more ("wide", a container
query) when it fits there, else dropped: the caption and the legend name it anyway.
"""
from __future__ import annotations

from typing import Any

from app.i18n import t
from app.services import deals

NARROW_PX = 210     # narrowest bar: /deals/ad at a 768 px window (300 px column - 88 px padding); viewer at 768 px ~247
WIDE_PX = 400       # bars at least this wide (drawer, desktop viewer ~420) also show the labels that fit there
TOP_PX, LANE_PX, GAP_PX = 7.4, 6.8, 6     # px per glyph: 12.5 px bold top lane / 11.5 px lane; free space between labels
COMPARABLE_EDGES = (0, 10, 25, 75, 90, 100)


def _span(x: float, text: str, px: float, bar_px: float, anchor: str | None = None) -> tuple[str, float, float]:
    """(anchor, left, right) in % of a bar of bar_px: centred on x, aligned to the edge where it would stick out
    (or the given anchor)."""
    w = len(text) * px / bar_px * 100
    anchor = anchor or ("l" if x - w / 2 < 0 else "r" if x + w / 2 > 100 else "c")
    return anchor, *{"l": (x, x + w), "r": (x - w, x), "c": (x - w / 2, x + w / 2)}[anchor]


def _row(labels: list[tuple[str, float, str]], px: float) -> list[dict[str, Any]]:
    """One row of labels (kind, x, text) in order of importance: those that keep GAP_PX clear of the ones before
    on the narrowest bar, then on a wide bar the rest that fit between them there ("wide": True)."""
    out: dict[int, dict[str, Any]] = {}
    for bar_px in (NARROW_PX, WIDE_PX):
        gap = GAP_PX / bar_px * 100
        taken = [_span(lb["x"], lb["text"], px, bar_px, lb["anchor"])[1:] for lb in out.values()]
        for i, (kind, x, text) in enumerate(labels):
            anchor, a, b = _span(x, text, px, bar_px)
            if i not in out and all(b + gap <= lo or a >= hi + gap for lo, hi in taken):
                taken.append((a, b))
                out[i] = {"kind": kind, "x": round(x, 1), "anchor": anchor, "text": text, "wide": bar_px != NARROW_PX}
    return [out[i] for i in sorted(out)]


def _eur(v: float) -> str:
    return "€ " + f"{round(v):,}".replace(",", ".")


def layout(p: dict[str, Any] | None, price_text: str | None = None) -> dict[str, Any] | None:
    """Drawing data of the price bar, x in % of the bar; None without a position. `price_text` is the label over
    the ring (the full bar); without it only the track is drawn (mini bars, the buy dialog)."""
    if not p or p.get("pos") is None:
        return None
    level = p.get("level")
    zoned = p.get("basis") == "geizhals" and p.get("zones") and p.get("axis")    # stored pre-zone results: no axis
    ring = round(p["pos"] * 100, 1)
    new_at = band = None
    if zoned:
        axis, new = p["axis"], p["new_price"]
        edges = [0.0, *(min(100.0, b * 100 / axis) for b in p["zones"]), 100.0]
        new_at = round(100 * 100 / axis, 1)
        if p.get("band"):
            low, high = p["band"]["low"] * 100 / new, p["band"]["high"] * 100 / new     # % of new
            band = {"from": round(min(99.0, max(0.0, low * 100 / axis)), 1),
                    "to": round(min(100.0, high * 100 / axis), 1), "off": high > axis}
    else:
        edges = list(COMPARABLE_EDGES)
    cut = [round(e, 1) for e in edges]
    segments = [{"level": n, "from": a, "to": b} for n, a, b in zip(deals.LEVEL_NAMES, cut, cut[1:], strict=False)
                if b > a]

    top = [("price", ring, price_text)] if price_text else []
    if new_at is not None:
        top.append(("new", new_at, t("New")))                          # next to the ring it is the legend's job
    if zoned:
        z, axis = p["zones"], p["axis"]
        i = deals.LEVEL_NAMES.index(level)
        own = [k for k in (i - 1, i) if 0 <= k < 4]                    # the cuts bounding the current zone first
        rest = sorted((k for k in range(4) if k not in own), key=lambda k: abs(z[k] * 100 / axis - ring))
        lane = [("cut", z[k] * 100 / axis, f"{z[k]} %") for k in own + rest if z[k] < axis]
    else:
        ticks = sorted(p.get("ticks") or [], key=lambda tk: tk[0] == 0.5)   # p25, p75, then the median
        lane = [("tick", at * 100, _eur(v)) for at, v in ticks]

    if price_text and zoned:
        aria = t("{price}: {level}, {n} % of the new price", price=price_text, level=deals.level_label(level),
                 n=p.get("pct"))
    elif price_text:
        aria = t("{price}: {level} among the comparable ads", price=price_text, level=deals.level_label(level))
    else:
        aria = deals.level_label(level)
    return {"basis": "geizhals" if zoned else "comparables", "level": level, "segments": segments, "ring": ring,
            "new_at": new_at, "band": band, "top": _row(top, TOP_PX), "lane": _row(lane, LANE_PX), "aria": aria}
