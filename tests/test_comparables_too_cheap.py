"""Comparable ads at a quarter of the new price are other, cheaper models: they do not rate a used item, the new price
does (five ads at 50–90 € made a 180 € item with a 299 € new price "very expensive")."""
from app.services import deals


def _comp(lo, med, hi):
    return {"count": 5, "min": lo, "p10": lo, "p25": lo + 5, "median": med, "p75": hi - 5, "p90": hi, "max": hi}


def test_far_too_cheap_comparables_leave_the_rating_to_the_new_price():
    p = deals.position(180, _comp(50, 70, 90), 298.98, condition="used")
    assert p["basis"] == "geizhals" and p["level"] == "fair" and p["pct"] == 60 and p["comparables_low"] == 23
    assert p["band"]["low"] == 55                       # still drawn as the cross-check


def test_a_used_market_at_half_the_new_price_still_decides():
    p = deals.position(78, _comp(70, 77, 85), 146, condition="used")
    assert p["basis"] == "comparables" and p["market"] is True


def test_new_items_are_rated_by_the_new_price_without_the_hint():
    p = deals.position(180, _comp(50, 70, 90), 298.98, condition="new")
    assert p["basis"] == "geizhals" and p["comparables_low"] is None
