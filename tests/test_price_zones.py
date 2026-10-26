"""The user's price zones (Settings → Prices → Price rating): storage, checks, the cents-exact rule and the page."""
import pytest
from fastapi.testclient import TestClient

from app.db import get_setting, set_setting
from app.services import price_zones as pz

NEW = 202.30


def test_defaults_when_unset_or_broken():
    assert pz.config() == pz.DEFAULTS
    set_setting(pz.KEY, "{not json")
    assert pz.config() == pz.DEFAULTS
    set_setting(pz.KEY, '{"used": [10, 20, 30, 40], "new": [5, 4, 3, 2]}')       # one bad group: its defaults only
    assert pz.config() == {"used": (10, 20, 30, 40), "new": pz.DEFAULTS["new"], "similar": pz.DEFAULTS["similar"]}
    set_setting(pz.KEY, '[1, 2, 3]')
    assert pz.config() == pz.DEFAULTS


@pytest.mark.parametrize("values, code", [
    ([0, 50, 80, 90], "used-range"), ([30, 50, 80, 301], "used-range"), (["x", 50, 80, 90], "used-range"),
    ([30, 50, 80], "used-range"), ([30, 50, 80, 90, 95], "used-range"), (["30.5", 50, 80, 90], "used-range"),
    ([30, 50, 50, 90], "used-order"), ([90, 80, 50, 30], "used-order"), ("30,50,80,90", "used-range"),
    # what int() would take but the page's script does not: the same text is refused on both sides
    (["1e2", 150, 200, 250], "used-range"), (["1_0", 50, 80, 90], "used-range"), (["+30", 50, 80, 90], "used-range"),
    (["٣٠", 50, 80, 90], "used-range"), (["0030", 50, 80, 90], "used-range"), ([True, 50, 80, 90], "used-range"),
])
def test_parse_refuses_with_a_code(values, code):
    with pytest.raises(pz.ZoneError) as e:
        pz.parse(values, "used")
    assert e.value.code == code and e.value.group == "used" and str(e.value) == pz.error_text(code)


def test_parse_accepts_padded_numbers():
    assert pz.parse([" 30 ", "50", 80, 90], "used") == (30, 50, 80, 90)
    assert pz.parse([1, 2, 3, 300], "new") == (1, 2, 3, 300)


def test_save_is_all_or_nothing_and_stores_only_changes():
    pz.save({"used": [20, 40, 70, 85], "new": list(pz.DEFAULTS["new"])})
    assert get_setting(pz.KEY) == '{"used": [20, 40, 70, 85]}'
    with pytest.raises(pz.ZoneError) as e:
        pz.save({"used": [10, 20, 30, 40], "new": [80, 70, 60, 50]})
    assert e.value.code == "new-order" and pz.config()["used"] == (20, 40, 70, 85)   # nothing saved
    pz.save(pz.DEFAULTS)
    assert get_setting(pz.KEY) == ""


def test_cent_boundaries_of_the_users_case():
    lim = (30, 50, 80, 90)                       # the zones the user's case was set up with
    assert [pz.level_for(p, NEW, lim) for p in (60.68, 60.69, 101.14, 101.15, 161.83, 161.84, 182.06, 182.07)] == \
        ["bargain", "cheap", "cheap", "fair", "fair", "expensive", "expensive", "very_expensive"]
    assert pz.share(139, NEW) == 68 and pz.share_exact(139, NEW) == pytest.approx(68.71, abs=0.01)
    assert [pz.price_from(NEW, b) for b in lim] == [60.69, 101.15, 161.84, 182.07]
    assert pz.group("new") == "new" and {pz.group(c) for c in ("like_new", "refurbished", "used", "defect", None)} == {"used"}


def test_default_zones_read_as_meant():
    """Used: 55-74 % is the usual resale price, from 90 % new is barely dearer. New: a private sale without return
    right is fair a bit below the lowest shop price, at that price it is expensive."""
    used, new = pz.DEFAULTS["used"], pz.DEFAULTS["new"]
    assert [pz.level_for(p, 100, used) for p in (34.99, 35, 54.99, 55, 74.99, 75, 89.99, 90)] == \
        ["bargain", "cheap", "cheap", "fair", "fair", "expensive", "expensive", "very_expensive"]
    assert [pz.level_for(p, 100, new) for p in (69.99, 70, 84.99, 85, 99.99, 100, 114.99, 115)] == \
        ["bargain", "cheap", "cheap", "fair", "fair", "expensive", "expensive", "very_expensive"]
    assert pz.level_for(139, NEW, used) == "fair"                                 # the user's case: 68 %


@pytest.mark.parametrize("new_price", [202.30, 199.99, 37.5])
@pytest.mark.parametrize("lim", [pz.DEFAULTS["used"], pz.DEFAULTS["new"], (85, 86, 87, 88)])
def test_floored_share_and_level_never_disagree(new_price, lim):
    """For every cent up to 3 × new: the whole % shown against the integer limits names the same label."""
    for c in range(1, round(new_price * 300) + 1):
        price = c / 100
        assert pz.NAMES[sum(pz.share(price, new_price) >= b for b in lim)] == pz.level_for(price, new_price, lim)
        if pz.level_for(price, new_price, lim) != pz.NAMES[0]:
            k = sum(pz.share(price, new_price) >= b for b in lim)
            assert price >= pz.price_from(new_price, lim[k - 1])      # the € limit of the caption agrees


def test_error_text_only_for_our_codes():
    assert pz.error_text("used-order") == "Used items: each limit must be higher than the one before."
    assert pz.error_text("new-range") == "New items: enter whole numbers from 1 to 300."
    assert pz.error_text("x") is None and pz.error_text("used-x") is None and pz.error_text(None) is None


# ------------------------------------------------------------------ the settings page
@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


def test_settings_page_and_navigation(client):
    page = client.get("/settings/prices")
    assert page.status_code == 200 and 'href="/settings/prices"' in page.text and "Price rating" in page.text
    assert 'x-data="priceZones(' in page.text and page.text.count('name="used"') == 4 and page.text.count('name="new"') == 4
    assert "35–54 %" in page.text and "from 90 %" in page.text and "below 70 %" in page.text   # readback chips
    assert 'title="35 / 55 / 75 / 90 %"' in page.text and 'title="70 / 85 / 100 / 115 %"' in page.text
    assert 'name="reset" value="1"' in page.text and 'formnovalidate :disabled="isDefault()"' in page.text
    from app.deals_web import price_zone_view
    from app.services import deals
    assert price_zone_view()["axis"] == [deals.AXIS_MIN, deals.AXIS_MAX] == [110, pz.HIGH + 10]   # the preview's scale
    assert ":class=\"{'input-error': bad('used', 1)}\"" in page.text            # the field that breaks the rule
    set_setting("ui_lang", "de")
    page = client.get("/settings/prices").text
    assert "Preiseinstufung" in page and ">Suche &amp; Preise<" in page and "Gebrauchtes" in page and "Neuware" in page


def test_settings_save_and_refuse(client):
    r = client.post("/settings/prices", data={"used": ["20", "40", "70", "85"], "new": ["75", "100", "110", "125"]},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/settings/prices?msg=")
    assert pz.config()["used"] == (20, 40, 70, 85)
    assert "Price zones saved" in client.get(r.headers["location"]).text
    r = client.post("/settings/prices", data={"used": ["50", "40", "70", "85"], "new": ["75", "100", "110", "125"]},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings/prices?err=used-order"
    assert pz.config()["used"] == (20, 40, 70, 85)                              # nothing saved
    set_setting("ui_lang", "de")
    assert "Gebrauchtes: jede Grenze muss höher sein als die davor." in client.get(r.headers["location"]).text
    page = client.get("/settings/prices", params={"err": "<script>alert(1)</script>"}).text
    assert "alert(1)" not in page and "jede Grenze" not in page


def test_settings_reset_to_defaults(client):
    """The reset button stores the defaults of all groups, whatever the fields hold (even broken ones)."""
    pz.save({"used": [20, 40, 70, 85], "new": [60, 80, 95, 110]})
    set_setting("ui_lang", "de")                                                # the message is translated at the post
    r = client.post("/settings/prices", data={"used": ["50", "x", "", "85"], "new": ["75"], "reset": "1"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/settings/prices?msg=")
    assert pz.config() == pz.DEFAULTS and get_setting(pz.KEY) == ""
    page = client.get(r.headers["location"]).text
    assert "Standardwerte wiederhergestellt" in page and "Auf Standardwerte zurücksetzen" in page
    assert "Alle Gruppen auf die Standardwerte zurücksetzen und speichern?" in page


def test_similar_ads_limits_are_saved_and_kept(client):
    """The limits for the rating by similar ads: a group of its own on the page; a form without it keeps them."""
    page = client.get("/settings/prices").text
    assert 'id="s-prices-similar"' in page and "% of the median" in page
    r = client.post("/settings/prices", data={"used": ["30", "50", "80", "90"], "new": ["75", "100", "115", "130"],
                                              "similar": ["60", "85", "120", "150"]}, follow_redirects=False)
    assert r.status_code == 303 and pz.config()["similar"] == (60, 85, 120, 150)
    pz.save({"used": [30, 50, 80, 90], "new": [75, 100, 115, 130]})
    assert pz.config()["similar"] == (60, 85, 120, 150)
    r = client.post("/settings/prices", data={"used": ["30", "50", "80", "90"], "new": ["75", "100", "115", "130"],
                                              "similar": ["60", "50", "120", "150"]}, follow_redirects=False)
    assert "err=similar-order" in r.headers["location"] and pz.config()["similar"] == (60, 85, 120, 150)
