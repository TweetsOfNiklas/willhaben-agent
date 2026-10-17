"""Kleinanzeigen and Vinted accounts in the app's browser: the login page in the remote view, the check in a hidden
tab (a redirect to the login page = logged out), the state on the site's settings page."""
from fastapi.testclient import TestClient

from app.services import site_accounts
from app.willhaben import session as session_mod


class _Sess:
    started = True

    def __init__(self, final):
        self.final, self.opened, self.loaded = final, [], []

    async def load_hidden(self, url, timeout_ms=30000):
        self.loaded.append(url)
        if isinstance(self.final, Exception):
            raise self.final
        return self.final

    async def open_url(self, url):
        self.opened.append(url)


async def test_check_reads_the_redirect(monkeypatch):
    sess = _Sess("https://www.kleinanzeigen.de/m-nachrichten.html")
    monkeypatch.setattr(session_mod, "get_session", lambda: sess)
    assert site_accounts.state("kleinanzeigen")["logged_in"] is None
    assert (await site_accounts.check("kleinanzeigen"))["logged_in"] is True
    sess.final = "https://www.kleinanzeigen.de/m-einloggen.html?targetUrl=/m-nachrichten.html"
    assert (await site_accounts.check("kleinanzeigen"))["logged_in"] is False
    sess.final = "https://www.vinted.at/member/signup/select_type?ref_url=%2Finbox"
    assert (await site_accounts.check("vinted"))["logged_in"] is False
    sess.final = TimeoutError("slow")
    st = await site_accounts.check("vinted")
    assert st["logged_in"] is None and "slow" in st["error"]
    await site_accounts.open_login("vinted")
    assert sess.opened == [site_accounts.SITES["vinted"].login_url]


def test_settings_page_shows_the_account_card(monkeypatch):
    from app.main import app

    sess = _Sess("https://www.vinted.at/inbox")
    monkeypatch.setattr(session_mod, "get_session", lambda: sess)
    with TestClient(app) as c:
        page = c.get("/settings/vinted").text
        assert 'data-site-account="vinted"' in page and "not checked yet" in page
        r = c.post("/settings/account/vinted/check", follow_redirects=False)
        assert r.headers["location"].startswith("/settings/vinted?msg=Logged")
        assert "Vinted account: logged in" in c.get("/settings/vinted").text
        r = c.post("/settings/account/vinted/login", follow_redirects=False)
        assert "remote=1" in r.headers["location"] and sess.opened
        assert c.post("/settings/account/ebay/check").status_code == 404
        assert "data-site-account" not in c.get("/settings/ebay").text


def test_capture_keeps_the_sites_requests():
    assert session_mod._other_site("https://gateway.kleinanzeigen.de/messagebox/api/x")
    assert session_mod._other_site("https://www.vinted.at/api/v2/inbox")
    assert not session_mod._other_site("https://www.example.com/kleinanzeigen.de")
