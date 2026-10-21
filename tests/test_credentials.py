"""Stored login details for willhaben and Vinted: kept with mode 600, the password never shown again, a blank
password keeps the stored one, and the settings card offers "Log in automatically" once they are there."""
import stat

from fastapi.testclient import TestClient

from app.config import get_settings
from app.services import credentials


def test_saved_private_and_never_shown(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    credentials.save("vinted", "me@example.org", "s3cret")
    p = tmp_path / "login-vinted.json"
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert credentials.stored("vinted") == {"user": "me@example.org", "has_password": True}
    credentials.save("vinted", "other@example.org", "")                   # blank: the password stays
    assert credentials.get("vinted") == {"user": "other@example.org", "password": "s3cret"}
    credentials.forget("vinted")
    assert credentials.get("vinted") is None


def test_the_settings_card(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    from app.db import set_setting
    from app.main import app
    set_setting("platform_vinted_enabled", "1")
    with TestClient(app) as c:
        c.post("/settings/credentials/vinted", data={"user": "me@example.org", "password": "s3cret"})
        page = c.get("/settings/vinted").text
        assert 'id="s-credentials"' in page and "me@example.org" in page and "s3cret" not in page
        assert "/settings/credentials/vinted/login" in page
        assert 'id="s-credentials"' in c.get("/settings/willhaben").text
        assert c.post("/settings/credentials/ebay", data={"user": "x", "password": "y"}).status_code == 404


async def test_typing_into_a_login_form(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    credentials.save("willhaben", "me@example.org", "s3cret")
    typed = []

    class Loc:
        def __init__(self, name):
            self.name, self.first = name, self

        def locator(self, *_):
            return self

        async def wait_for(self, **kw):
            pass

        async def click(self):
            typed.append((self.name, "click"))

        async def fill(self, v):
            pass

        async def press_sequentially(self, text, delay=0):
            typed.append((self.name, text))

        async def count(self):
            return 1

        async def press(self, key):
            pass

    class Page:
        def locator(self, sel):
            return Loc("password" if "password" in sel else "submit" if "submit" in sel else "user")

        async def wait_for_timeout(self, ms):
            pass
    await credentials.type_in(Page(), "willhaben")
    assert ("user", "me@example.org") in typed and ("password", "s3cret") in typed and ("submit", "click") in typed
