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
    # the popover: the same block for every site, chips to switch, each with its own budget form
    assert "site = 'kleinanzeigen'" in page and "site = 'shpock'" in page and "site = 'vinted'" not in page
    assert 'href="/stats/requests?platform=kleinanzeigen"' in page and 'href="/stats/requests?platform=all"' in page
    assert 'action="/settings/platform/kleinanzeigen/budget"' in page and "Kleinanzeigen requests" in page
    assert "1 blocked" in page
    assert "· Kleinanzeigen 12 / 90" in page                            # collapsed rail: the ring's tooltip


def test_no_other_site_no_extra_rows():
    from app.main import app

    with TestClient(app) as c:
        page = c.get("/myads").text
    assert "data-site-usage" not in page and "site = '" not in page


def test_the_phone_menu_shows_the_meters():
    set_setting("platform_kleinanzeigen_enabled", "1")
    _count("kleinanzeigen", 12)
    from app.main import app

    with TestClient(app) as c:
        page = c.get("/myads").text
    menu = page[page.index('id="account-menu"'):page.index('id="usage-pop"')]      # "More" in the phone tab bar
    assert "willhaben / h" in menu and 'data-site-usage="kleinanzeigen"' in menu and "md:hidden" in menu
    assert "pop($event, 'usage')" in menu                                  # a tap opens the usage popover


def test_site_budgets_follow_willhabens_until_set_apart():
    from app.db import platform_per_hour

    assert platform_per_hour("vinted") == platform_per_hour("ebay") == platform_per_hour("willhaben") == 240
    set_setting("requests_per_hour", "300")
    assert platform_per_hour("kleinanzeigen") == 300
    set_setting("platform_ebay_per_hour", "50")
    assert platform_per_hour("ebay") == 50 and platform_per_hour("vinted") == 300


def test_an_old_default_the_form_saved_follows_willhaben_after_the_update():
    from app.db import _follow_willhaben_budget, get_setting

    set_setting("site_budgets_follow_wh", "")
    set_setting("platform_vinted_per_hour", "40")          # the old default, stored by a Save
    set_setting("platform_shpock_per_hour", "75")          # chosen
    _follow_willhaben_budget()
    assert get_setting("platform_vinted_per_hour") == "" and get_setting("platform_shpock_per_hour") == "75"
    set_setting("platform_vinted_per_hour", "40")          # chosen after the update: stays
    _follow_willhaben_budget()
    assert get_setting("platform_vinted_per_hour") == "40"


def test_the_budget_field_is_empty_while_the_site_follows_willhaben():
    from app.main import app
    from app.platforms import kleinanzeigen as ka
    from app.willhaben.public import willhaben_limiter

    before = willhaben_limiter().per_hour, ka.kleinanzeigen_limiter().per_hour
    try:
        with TestClient(app) as c:
            page = c.get("/settings/kleinanzeigen").text
            assert 'name="budget" value="" placeholder="as willhaben: 240"' in page
            c.post("/settings/willhaben", data={"requests_per_hour": "300"}, follow_redirects=False)
            assert ka.kleinanzeigen_limiter().per_hour == 300                  # follows the new willhaben budget
            c.post("/settings/platform/kleinanzeigen", data={"enabled": "on", "budget": "90"}, follow_redirects=False)
            c.post("/settings/willhaben", data={"requests_per_hour": "200"}, follow_redirects=False)
            assert ka.kleinanzeigen_limiter().per_hour == 90                   # its own budget stays
            assert 'name="budget" value="90" placeholder="as willhaben: 200"' in c.get("/settings/kleinanzeigen").text
    finally:
        willhaben_limiter().per_hour, ka.kleinanzeigen_limiter().per_hour = before


def test_the_popover_saves_one_sites_budget_only():
    set_setting("platform_kleinanzeigen_enabled", "1")
    set_setting("platform_kleinanzeigen_min_gap_s", "7")
    from app.db import get_setting
    from app.main import app

    with TestClient(app) as c:
        r = c.post("/settings/platform/kleinanzeigen/budget", data={"budget": "120", "next": "/hits"}, follow_redirects=False)
        assert r.headers["location"] == "/hits"
        assert get_setting("platform_kleinanzeigen_per_hour") == "120"
        assert get_setting("platform_kleinanzeigen_enabled") == "1" and get_setting("platform_kleinanzeigen_min_gap_s") == "7"
        c.post("/settings/platform/kleinanzeigen/budget", data={"budget": ""})
        assert get_setting("platform_kleinanzeigen_per_hour") == "-"                  # empty: as willhaben
