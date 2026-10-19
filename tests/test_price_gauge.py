from app.services.price_gauge import gauge
from tests.test_dashboard import client  # noqa: F401  (pytest fixture)

STATS = {"count": 7, "min": 800.0, "p25": 980.0, "median": 1100.0, "p75": 1250.0, "max": 1500.0}


def test_gauge_verdicts_and_positions():
    g = gauge(957, STATS)
    assert g["verdict"] == "low" and g["diff_pct"] == -13 and 0 < g["pos"] < g["median_pos"] < 100
    assert gauge(1100, STATS)["verdict"] == "fair" and gauge(1100, STATS)["diff_pct"] == 0
    assert gauge(1400, STATS)["verdict"] == "high" and gauge(1400, STATS)["pos"] > gauge(1100, STATS)["pos"]
    assert 0 <= gauge(5000, STATS)["pos"] <= 100 and 0 <= gauge(10, STATS)["pos"] <= 100   # outliers stay on the bar


def test_no_gauge_without_data():
    assert gauge(None, STATS) is None and gauge(100, None) is None
    assert gauge(100, {**STATS, "count": 2}) is None


def test_gauge_renders_on_the_draft_page(client):  # noqa: F811
    from app import db as dbmod
    with dbmod.session() as db:
        d = dbmod.AdDraft(input_text="", title="T", description="D", image_paths=[], price_suggestion=957,
                          price_stats=STATS)
        db.add(d)
        db.commit()
        db.refresh(d)
    html = client.get(f"/new-ad/{d.id}").text
    assert "deal-track" in html and "13 % below the median" in html and "cheap" in html
