"""A site's login in session cookies survives a restart of the app's browser: kept at a successful check, given
back when the browser starts, deleted with "Reset" (services/site_accounts)."""
from app.services import site_accounts


class Ctx:
    def __init__(self, cookies):
        self.jar = list(cookies)

    async def cookies(self):
        return list(self.jar)

    async def add_cookies(self, cs):
        self.jar += cs

    async def clear_cookies(self, domain=None):
        self.jar = [c for c in self.jar if not domain.search(c["domain"].lstrip("."))]


SESSION = {"name": "access_token", "value": "x", "domain": ".kleinanzeigen.de", "path": "/", "expires": -1}
LASTING = {"name": "consent", "value": "y", "domain": ".kleinanzeigen.de", "path": "/", "expires": 2_000_000_000}
OTHER = {"name": "sid", "value": "z", "domain": ".willhaben.at", "path": "/", "expires": -1}


async def test_session_cookies_survive_a_restart(monkeypatch):
    class Sess:
        context = Ctx([SESSION, LASTING, OTHER])

        def _require(self):
            pass
    sess = Sess()
    monkeypatch.setattr("app.willhaben.session.get_session", lambda: sess)
    assert await site_accounts.keep_cookies("kleinanzeigen") == 1               # the session cookie only
    fresh = Ctx([LASTING])                                                       # after a restart
    assert await site_accounts.restore_cookies(fresh) == 1
    assert {c["name"] for c in fresh.jar} == {"consent", "access_token"}
    assert await site_accounts.restore_cookies(fresh) == 0                       # nothing twice
    await site_accounts.forget("kleinanzeigen")                                  # "Reset": the file goes too
    assert not site_accounts._cookie_file("kleinanzeigen").exists()
    assert await site_accounts.restore_cookies(Ctx([])) == 0
