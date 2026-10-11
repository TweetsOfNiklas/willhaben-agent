"""Settings → Web address: stored in the app, it wins over PUBLIC_URL in .env for every link (no restart)."""
from app.config import address_check, get_settings
from app.db import SETTING_PUBLIC_URL, apply_public_url, get_setting, set_setting
from tests.test_dashboard import client  # noqa: F401  (client fixture)


def test_address_check():
    assert address_check("http://localhost:8000") == {"reachable": False, "https": False}
    assert address_check("http://127.0.0.1:8000") == {"reachable": False, "https": False}
    assert address_check("http://homeserver:8000") == {"reachable": True, "https": False}
    assert address_check("https://wh.example.com") == {"reachable": True, "https": True}
    assert address_check("") == {"reachable": False, "https": False}


def test_saved_address_wins_and_reset_falls_back(client):  # noqa: F811
    env = type(get_settings())().public_url
    try:
        page = client.get("/settings/web").text
        assert 'name="public_url"' in page and 'href="/settings/web"' in page
        client.post("/settings/web", data={"public_url": "http://localhost:8000"})
        page = client.get("/settings/web").text
        assert "only works on this computer" in page and 'rounded-full bg-warning' in page  # warning dot in the nav
        r = client.post("/settings/web", data={"public_url": " https://wh.example.com/ "}, follow_redirects=False)
        assert r.status_code == 303 and "saved" in r.headers["location"]
        assert get_setting(SETTING_PUBLIC_URL) == "https://wh.example.com"
        assert get_settings().public_url == "https://wh.example.com"     # every link builder sees it at once
        from app.services import purchase
        assert purchase._base() == "https://wh.example.com"
        assert "Links in notifications open https://wh.example.com." in client.get("/settings/web").text
        assert "only with an https://" not in client.get("/settings/telegram").text
        client.post("/settings/web", data={"public_url": ""})
        assert get_setting(SETTING_PUBLIC_URL) == "" and get_settings().public_url == env
    finally:
        set_setting(SETTING_PUBLIC_URL, "")
        apply_public_url()


def test_invalid_address_is_refused(client):  # noqa: F811
    for bad in ("homeserver:8000", "ftp://x", "https://wh.example.com?x=1", "https://has space.at"):
        r = client.post("/settings/web", data={"public_url": bad}, follow_redirects=False)
        assert "http%3A%2F%2F" in r.headers["location"] or "http://" in r.headers["location"]
        assert get_setting(SETTING_PUBLIC_URL) == ""


def test_stored_address_applies_at_startup():
    try:
        set_setting(SETTING_PUBLIC_URL, "http://homeserver:8000")
        assert apply_public_url() == "http://homeserver:8000" == get_settings().public_url
    finally:
        set_setting(SETTING_PUBLIC_URL, "")
        apply_public_url()


def test_telegram_and_ha_sections_point_to_the_setting(client):  # noqa: F811
    assert 'href="/settings/web"' in client.get("/settings/telegram").text
    assert 'href="/settings/web"' in client.get("/settings/ha").text
