"""The price bar's drawing data (price_bar.layout): zones, markers, the comparables' strip and labels that never touch."""
import pytest

from app.services import deals, price_bar
from app.services import price_zones as pz

USER_CASE = [120, 135, 150, 170]


def _eur(v):
    return f"€ {v:,.0f}".replace(",", ".") if v == int(v) else f"€ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _spans(labels, px, bar_px):
    out = []
    for lb in labels:
        w = len(lb["text"]) * px / bar_px * 100
        a = {"c": lb["x"] - w / 2, "l": lb["x"], "r": lb["x"] - w}[lb["anchor"]]
        out.append((a, a + w))
    return out


def _no_touch(labels, px):
    """No two labels closer than GAP_PX: those of the narrowest bar there, all of them on a wide bar."""
    for bar_px, shown in ((price_bar.NARROW_PX, [lb for lb in labels if not lb["wide"]]), (price_bar.WIDE_PX, labels)):
        gap = price_bar.GAP_PX / bar_px * 100
        spans = sorted(_spans(shown, px, bar_px))
        assert all(a >= -0.06 and b <= 100.06 for a, b in spans), spans           # inside the bar (rounding of x)
        if not all(b1 + gap <= a2 + 0.11 for (_, b1), (a2, _) in zip(spans, spans[1:], strict=False)):
            return False
    return True


def test_users_case_geometry():
    b = price_bar.layout(deals.position(139, deals.stats(USER_CASE), 202.30), "€ 139")
    assert [(s["level"], s["from"], s["to"]) for s in b["segments"]] == [
        ("bargain", 0, 27.3), ("cheap", 27.3, 45.5), ("fair", 45.5, 72.7), ("expensive", 72.7, 81.8),
        ("very_expensive", 81.8, 100)]
    assert b["ring"] == 62.5 and b["new_at"] == 90.9 and b["level"] == "fair" and b["basis"] == "geizhals"
    assert b["band"] == {"from": 59.0, "to": 69.7, "off": False}
    assert [lb["text"] for lb in b["top"]] == ["€ 139", "New"]
    assert [(lb["text"], lb["wide"]) for lb in sorted(b["lane"], key=lambda lb: lb["x"])] == [
        ("30 %", False), ("50 %", False), ("80 %", False), ("90 %", True)]           # 90 % only on a wide bar
    assert all(lb["kind"] == "cut" for lb in b["lane"])
    assert b["aria"] == "€ 139: fair, 68 % of the new price"


def test_new_label_gives_way_to_the_price():
    b = price_bar.layout(deals.position(195.02, None, 199.0, "new"), "€ 195,02")
    assert b["ring"] == 70.0 and b["new_at"] == 71.4 and [lb["text"] for lb in b["top"]] == ["€ 195,02"]


@pytest.mark.parametrize("group", ["used", "new"])
def test_segments_are_contiguous(group):
    cond = "new" if group == "new" else None
    for price in [1, 10, 50, 99, 100, 150, 199, 250, 299, 300, 450, 700]:
        for p in (deals.position(price, None, 100.0, cond), deals.position(price, deals.stats([40, 60, 80, 90, 100, 200]), None)):
            b = price_bar.layout(p, _eur(price))
            segs = b["segments"]
            assert segs[0]["from"] == 0 and segs[-1]["to"] == 100
            assert all(s["to"] > s["from"] for s in segs)
            assert all(x["to"] == y["from"] for x, y in zip(segs, segs[1:], strict=False))
            assert sum(s["to"] - s["from"] for s in segs) == pytest.approx(100)
    tight = deals.position(150, None, 100.0, None, {"used": (1, 2, 3, 300), "new": pz.DEFAULTS["new"]})
    segs = price_bar.layout(tight)["segments"]                                     # 300 % = the axis end: dropped
    assert [s["level"] for s in segs] == ["bargain", "cheap", "fair", "expensive"] and segs[-1]["to"] == 100


@pytest.mark.parametrize("group", ["used", "new"])
@pytest.mark.parametrize("new_price", [202.30, 12999.0])
def test_labels_never_touch_at_the_narrowest_bar(group, new_price):
    cond = "new" if group == "new" else None
    for pct in range(1, 301):
        price = round(new_price * pct / 100, 2)
        p = deals.position(price, None, new_price, cond)
        b = price_bar.layout(p, _eur(price))
        assert _no_touch(b["top"], price_bar.TOP_PX), (pct, b["top"])
        assert _no_touch(b["lane"], price_bar.LANE_PX), (pct, b["lane"])
        assert b["top"][0]["kind"] == "price" and not b["top"][0]["wide"]          # the price label always stays
        own = [z for z in p["zones"] if z < p["axis"]]
        assert {lb["text"] for lb in b["lane"]} <= {f"{z} %" for z in own}


def test_off_scale_band_is_clipped():
    b = price_bar.layout(deals.position(140, deals.stats([130, 140, 150, 160, 170]), 100.0), "€ 140")
    assert b["band"]["to"] == 100 and b["band"]["from"] <= 99 and b["band"]["off"] is True
    far = price_bar.layout(deals.position(60, deals.stats([300, 320, 340]), 100.0), "€ 60")   # 3 ads: no mismatch rule
    assert far["band"] == {"from": 99.0, "to": 100.0, "off": True}


def test_comparables_ticks():
    b = price_bar.layout(deals.position(105, deals.stats([40, 60, 80, 90, 100, 110, 120, 130, 150, 200]), None), "€ 105")
    assert [s["from"] for s in b["segments"]] == [0, 10, 25, 75, 90] and b["new_at"] is None and b["band"] is None
    assert [(lb["text"], lb["x"]) for lb in b["lane"]] == [("€ 82", 25.0), ("€ 128", 75.0), ("€ 105", 50.0)]
    assert b["aria"] == "€ 105: fair among the comparable ads"
    tied = price_bar.layout(deals.position(100, deals.stats([100] * 5 + [200]), None), "€ 100")
    assert [(lb["text"], lb["x"], lb["kind"]) for lb in tied["lane"]] == [("€ 100", 50.0, "tick")]
    assert price_bar.layout({"pos": None, "reason": "defect"}) is None and price_bar.layout(None) is None


def test_mini_bar_of_a_stored_card_without_zones():
    """A product-search card stored before the zones: an old new-price position draws on the comparables segments."""
    b = price_bar.layout({"pos": 0.4, "level": "fair", "basis": "geizhals", "ticks": [[0.25, 40]]})
    assert b["basis"] == "comparables" and b["new_at"] is None and b["top"] == [] and b["aria"] == "fair"
