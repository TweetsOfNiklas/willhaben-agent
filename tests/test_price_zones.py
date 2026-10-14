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
    assert pz.config() == {"used": (10, 20, 30, 40), "new": pz.DEFAULTS["new"]}
    set_setting(pz.KEY, '[1, 2, 3]')
    assert pz.config() == pz.DEFAULTS


@pytest.mark.parametrize("values, code", [
    ([0, 50, 80, 90], "used-range"), ([30, 50, 80, 301], "used-range"), (["x", 50, 80, 90], "used-range"),
    ([30, 50, 80], "used-range"), ([30, 50, 80, 90, 95], "used-range"), (["30.5", 50, 80, 90], "used-range"),
    ([30, 50, 50, 90], "used-order"), ([90, 80, 50, 30], "used-order"), ("30,50,80,90", "used-range"),
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
    lim = pz.DEFAULTS["used"]
    assert [pz.level_for(p, NEW, lim) for p in (60.68, 60.69, 101.14, 101.15, 161.83, 161.84, 182.06, 182.07)] == \
        ["bargain", "cheap", "cheap", "fair", "fair", "expensive", "expensive", "very_expensive"]
    assert pz.share(139, NEW) == 68 and pz.share_exact(139, NEW) == pytest.approx(68.71, abs=0.01)
    assert [pz.price_from(NEW, b) for b in lim] == [60.69, 101.15, 161.84, 182.07]
    assert pz.group("new") == "new" and {pz.group(c) for c in ("like_new", "refurbished", "used", "defect", None)} == {"used"}


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
    assert "30–49 %" in page.text and "from 90 %" in page.text and "below 75 %" in page.text   # readback chips
    set_setting("ui_lang", "de")
    page = client.get("/settings/prices").text
    assert "Preiseinstufung" in page and ">Preise<" in page and "Gebrauchtes" in page and "Neuware" in page


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
