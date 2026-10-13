"""The sidebar meter and its popover count the other switched-on sites' requests of the last hour, too."""
import time

from fastapi.testclient import TestClient

from app.db import AccessCount, session, set_setting


def _count(platform, n, outcome="ok", minutes_ago=5):
    with session() as db:
        db.add(AccessCount(minute=int(time.time() // 60) - minutes_ago, platform=platform, channel="anonymous",
                           kind="search", outcome=outcome, count=n))
        db.commit()


def test_meter_lists_the_other_sites():
    set_setting("platform_kleinanzeigen_enabled", "1")
    set_setting("platform_kleinanzeigen_per_hour", "90")
    set_setting("platform_vinted_enabled", "0")
    _count("kleinanzeigen", 12)
    _count("kleinanzeigen", 30, outcome="cached")                # cache hits cost nothing: not counted
    _count("kleinanzeigen", 50, minutes_ago=90)                  # older than an hour
    _count("shpock", 3)                                          # switched off but used within the hour: shown
    _count("shpock", 1, outcome="blocked")                       # a 403/429 answer counts and is named
    from app.main import app

    with TestClient(app) as c:
        page = c.get("/myads").text
    row = page[page.index('data-site-usage="kleinanzeigen"'):]
    row = row[:row.index("</a>")]
    assert "Kleinanzeigen / h" in row and ">12<" in row and "/ 90" in row and "width:13%" in row
    assert "12 / 90 Kleinanzeigen requests in the last hour" in page
    assert 'data-site-usage="shpock"' in page and 'data-site-usage="vinted"' not in page
    assert 'data-site-usage="willhaben"' not in page                    # willhaben keeps its own row
    assert "Other sites" in page and 'href="/stats/requests?platform=kleinanzeigen"' in page
    assert 'href="/settings/kleinanzeigen">Budgets per site' in page and 'href="/stats/requests?platform=all"' in page
    assert "· 1 blocked" in page
    assert "· Kleinanzeigen 12 / 90" in page                            # collapsed rail: the ring's tooltip


def test_no_other_site_no_extra_rows():
    from app.main import app

    with TestClient(app) as c:
        page = c.get("/myads").text
    assert "data-site-usage" not in page and "Other sites" not in page
