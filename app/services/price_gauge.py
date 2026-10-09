"""Price gauge: where a price sits among comparable prices (green = low … red = high), like a portal's price bar."""
from __future__ import annotations

from typing import Any


def gauge(price: float | None, stats: dict[str, Any] | None) -> dict[str, Any] | None:
    """Positions in % of the bar for the price and the median, the verdict and the difference to the median.

    stats: price_stats() output (count, min, p25, median, p75, max). None when there is nothing to compare.
    """
    if not price or not stats or not stats.get("median") or (stats.get("count") or 0) < 3:
        return None
    lo = min(float(stats["min"]), price) * 0.95
    hi = max(float(stats["max"]), price) * 1.05
    span = (hi - lo) or 1.0

    def pos(v: float) -> float:
        return round(max(0.0, min(100.0, (v - lo) / span * 100)), 1)

    median = float(stats["median"])
    verdict = "low" if price < float(stats["p25"]) else "high" if price > float(stats["p75"]) else "fair"
    return {"price": price, "pos": pos(price), "median_pos": pos(median), "p25_pos": pos(float(stats["p25"])),
            "p75_pos": pos(float(stats["p75"])), "verdict": verdict, "count": stats["count"],
            "diff_pct": round((price - median) / median * 100), "median": median}
