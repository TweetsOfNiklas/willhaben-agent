"""The one-time Vinted inbox exploration: read-only hidden tabs (inbox, then its newest conversation), Vinted's data
calls kept in a local capture file, once per version."""
import json
from types import SimpleNamespace

from app.platforms import vinted_inbox


class Sess:
    def __init__(self, tmp):
        self.started, self.opened = True, []
        self.settings = SimpleNamespace(captures_dir=tmp)

    async def read_hidden(self, url, wait_for, timeout_ms=0, keep=None, allow=None, deny=None):
        self.opened.append(url)
        assert allow("POST", "https://www.vinted.at/web/api/auth/refresh") and not allow("POST", "https://www.vinted.at/api/v2/conversations/1/replies")
        assert deny("GET", "https://www.vinted.at/web/api/auth/expire-cookies") and not deny("GET", "https://www.vinted.at/inbox")
        assert keep("https://www.vinted.at/api/v2/inbox?page=1") and not keep("https://www.willhaben.at/webapi/x")
        if url.endswith("/inbox"):
            return {"text": "Posteingang", "found": True, "bodies": {"https://www.vinted.at/api/v2/inbox": '{"conversations":[{"id":12345678}]}'},
                    "requests": [{"method": "GET", "url": "https://www.vinted.at/api/v2/inbox?page=1", "status": 200}]}
        return {"text": "Nachricht", "found": True, "bodies": {}, "requests": []}


async def test_explore_reads_inbox_and_a_conversation(tmp_path, monkeypatch):
    sess = Sess(tmp_path)
    monkeypatch.setattr("app.willhaben.session.get_session", lambda: sess)
    await vinted_inbox.explore()
    assert sess.opened == ["https://www.vinted.at/inbox", "https://www.vinted.at/inbox/12345678"]
    saved = json.loads(next(tmp_path.glob("*-vinted-inbox-explore.json")).read_text())
    assert saved["conversation_id"] == "12345678"


def test_the_bot_protection_and_the_token_refresh_pass_writes_do_not():
    allow = vinted_inbox._allow
    assert allow("POST", "https://dd.vinted.lt/js") and allow("POST", "https://api.vinted.at/tracking-events/batch")
    assert allow("POST", "https://www.vinted.at/web/api/auth/refresh")
    assert not allow("POST", "https://www.vinted.at/api/v2/conversations/1/replies")
    assert not allow("PUT", "https://www.vinted.at/api/v2/conversations/1/read")
    assert not allow("POST", "https://www.vinted.at/api/v2/transactions/1/checkout")

def test_summary_names_a_missing_login():
    out = {"inbox": {"requests": [{"url": "https://www.vinted.at/member/signup/select_type?ref_url=%2Finbox",
                                   "type": "document", "status": 307}]}, "conversation": None}
    assert "not logged in" in vinted_inbox.summary(out)
    ok = {"inbox": {"requests": [{"url": "https://www.vinted.at/api/v2/inbox", "type": "fetch", "status": 200}]},
          "conversation": {"requests": []}}
    assert vinted_inbox.summary(ok) == "Vinted inbox read (1 data calls), conversation read"


def test_the_login_button_picks_the_platform(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import site_accounts
    opened = []

    async def open_login(pid):
        opened.append(pid)
    monkeypatch.setattr(site_accounts, "open_login", open_login)
    with TestClient(app) as c:
        c.post("/session/login", data={"site": "vinted", "next": "/session"}, follow_redirects=False)
    assert opened == ["vinted"]


async def test_reset_deletes_only_the_sites_cookies(monkeypatch):
    from app.services import site_accounts

    jar = [{"name": "datadome", "domain": ".vinted.at"}, {"name": "access_token_web", "domain": "www.vinted.at"},
           {"name": "x", "domain": "dd.vinted.lt"}, {"name": "bbxauth", "domain": ".willhaben.at"}]

    class Ctx:
        async def cookies(self):
            return list(jar)

        async def clear_cookies(self, domain=None):
            jar[:] = [c for c in jar if not domain.search(c["domain"].lstrip("."))]

    sess = SimpleNamespace(context=Ctx(), _require=lambda: None)
    monkeypatch.setattr("app.willhaben.session.get_session", lambda: sess)
    assert await site_accounts.forget("vinted") == 3
    assert jar == [{"name": "bbxauth", "domain": ".willhaben.at"}]
    assert site_accounts.state("vinted")["logged_in"] is False
