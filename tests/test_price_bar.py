"""The price bar's drawing data (price_bar.layout): zones, markers, the comparables' strip and labels that never touch."""
import pytest

from app.services import deals, price_bar
from app.services import price_zones as pz

USER_CASE = [120, 135, 150, 170]
# the zones these cases were drawn with (the defaults until 2026-10-14): the geometry stays put when the defaults move
ZONES = {"used": (30, 50, 80, 90), "new": (75, 100, 110, 125)}


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
    """No two labels closer than GAP_PX on each bar width: those shown from that width on (from_px)."""
    for bar_px in price_bar.TIERS:
        shown = [lb for lb in labels if (lb["from_px"] or 0) <= bar_px]
        gap = price_bar.GAP_PX / bar_px * 100
        spans = sorted(_spans(shown, px, bar_px))
        assert all(a >= -0.06 and b <= 100.06 for a, b in spans), spans           # inside the bar (rounding of x)
        if not all(b1 + gap <= a2 + 0.11 for (_, b1), (a2, _) in zip(spans, spans[1:], strict=False)):
            return False
    return True


def test_users_case_geometry():
    b = price_bar.layout(deals.position(139, deals.stats(USER_CASE), 202.30, zones=ZONES), "€ 139")
    assert [(s["level"], s["from"], s["to"]) for s in b["segments"]] == [
        ("bargain", 0, 27.3), ("cheap", 27.3, 45.5), ("fair", 45.5, 72.7), ("expensive", 72.7, 81.8),
        ("very_expensive", 81.8, 100)]
    assert b["ring"] == 62.5 and b["new_at"] == 90.9 and b["level"] == "fair" and b["basis"] == "geizhals"
    assert b["band"] == {"from": 59.0, "to": 69.7, "off": False, "out": False, "text": "€ 131–€ 155"}
    assert [lb["text"] for lb in b["top"]] == ["€ 139", "New"]
    assert [(lb["text"], lb["from_px"]) for lb in sorted(b["lane"], key=lambda lb: lb["x"])] == [
        ("30 %", None), ("50 %", None), ("80 %", None), ("90 %", 400)]               # 90 % only on a wide bar
    assert all(lb["kind"] == "cut" for lb in b["lane"])
    assert b["aria"] == "€ 139: fair, 68 % of the new price"


def test_new_label_gives_way_to_the_price():
    """Next to the ring "New" moves under the marker, first in the lane (the "100 %" cut there gives way)."""
    b = price_bar.layout(deals.position(195.02, None, 199.0, "new", zones=ZONES), "€ 195,02")
    assert b["ring"] == 70.0 and b["new_at"] == 71.4 and [lb["text"] for lb in b["top"]] == ["€ 195,02"]
    assert b["lane"][0] == {"kind": "new", "x": 71.4, "anchor": "c", "text": "New", "from_px": None}
    assert "100 %" not in [lb["text"] for lb in b["lane"]] and "75 %" in [lb["text"] for lb in b["lane"]]


@pytest.mark.parametrize("price, new_price, text", [(14999, 12999.0, "€ 14.999"), (1049.90, 999.0, "€ 1.049,90"),
                                                    (259, 199.0, "€ 259")])
def test_a_price_label_over_the_marker_never_passes_for_the_new_price(price, new_price, text):
    """The price label right-aligned to a ring near the end reaches over the marker: "New" is then named under it,
    on every bar (also phones at 260 px, where the top lane's check at 210 px dropped it before)."""
    p = deals.position(price, None, new_price, "new" if new_price == 199.0 else None)
    b = price_bar.layout(p, text)
    assert [lb["text"] for lb in b["top"]] == [text]
    new = [lb for lb in b["lane"] if lb["kind"] == "new"]
    assert new == [{"kind": "new", "x": b["new_at"], "anchor": "c", "text": "New", "from_px": None}]


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
    segs = price_bar.layout(tight)["segments"]                     # the axis ends 10 % past the highest limit
    assert [s["level"] for s in segs] == ["bargain", "cheap", "fair", "expensive", "very_expensive"]
    assert segs[-1] == {"level": "very_expensive", "from": 96.8, "to": 100}


def test_the_highest_limit_keeps_its_zone_and_label():
    zones = {"used": (100, 150, 200, 300), "new": pz.DEFAULTS["new"]}
    p = deals.position(400, None, 100.0, None, zones)
    b = price_bar.layout(p, "€ 400")
    assert p["level"] == "very_expensive" and p["axis"] == deals.AXIS_MAX == pz.HIGH + 10
    assert b["segments"][-1]["level"] == "very_expensive" and b["ring"] == 100
    assert "300 %" in [lb["text"] for lb in b["lane"]]
    assert b["top"][0]["text"] == "€ 400 ›"                         # off the end of the scale
    assert price_bar.layout(deals.position(250, None, 100.0, None, zones), "€ 250")["top"][0]["text"] == "€ 250"


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
        assert b["top"][0]["kind"] == "price" and b["top"][0]["from_px"] is None   # the price label always stays
        own = [z for z in p["zones"] if z < p["axis"]]
        assert {lb["text"] for lb in b["lane"]} <= {f"{z} %" for z in own} | {"New"}
        new = [lb for lb in b["top"] + b["lane"] if lb["kind"] == "new"]           # the marker is always named
        assert len(new) == 1 and new[0]["from_px"] is None and new[0]["x"] == b["new_at"], (pct, b)


def test_off_scale_band_is_clipped():
    b = price_bar.layout(deals.position(140, deals.stats([130, 140, 150, 160, 170]), 100.0), "€ 140")
    assert b["band"]["to"] == 100 and b["band"]["from"] <= 99 and b["band"]["off"] is True
    assert b["band"]["out"] is False                                # "reaches past the scale", not "outside"
    far = price_bar.layout(deals.position(60, deals.stats([300, 320, 340]), 100.0), "€ 60")   # 3 ads: no mismatch rule
    assert far["band"] == {"from": 99.0, "to": 100.0, "off": True, "out": True, "text": "€ 310–€ 330"}


def test_comparables_ticks():
    b = price_bar.layout(deals.position(105, deals.stats([40, 60, 80, 90, 100, 110, 120, 130, 150, 200]), None), "€ 105")
    assert [s["from"] for s in b["segments"]] == [0, 10, 25, 75, 90] and b["new_at"] is None and b["band"] is None
    # half up as the caption's "€ 82,50–€ 127,50"; the median's "€ 105" would repeat the price over the ring
    assert [(lb["text"], lb["x"]) for lb in b["lane"]] == [("€ 83", 25.0), ("€ 128", 75.0)]
    assert b["aria"] == "€ 105: fair among the comparable ads"
    b = price_bar.layout(deals.position(100, deals.stats([40, 60, 80, 90, 100, 110, 120, 130, 150, 200]), None), "€ 100")
    assert [lb["text"] for lb in b["lane"]] == ["€ 83", "€ 128", "€ 105"]
    p = deals.position(400, deals.stats([380, 392.5, 402.5, 412.5, 420]), None)
    assert [v for _, v in p["ticks"]] == [393, 403, 413]               # round() would give 392, 402, 412
    tied = price_bar.layout(deals.position(100, deals.stats([100] * 5 + [200]), None), "€ 100")
    assert [(lb["text"], lb["x"], lb["kind"]) for lb in tied["lane"]] == [("€ 100", 50.0, "tick")]
    assert price_bar.layout({"pos": None, "reason": "defect"}) is None and price_bar.layout(None) is None


def test_mini_bar_of_a_stored_card_without_zones():
    """A product-search card stored before the zones: an old new-price position draws on the comparables segments."""
    b = price_bar.layout({"pos": 0.4, "level": "fair", "basis": "geizhals", "ticks": [[0.25, 40]]})
    assert b["basis"] == "comparables" and b["new_at"] is None and b["top"] == [] and b["aria"] == "fair"
