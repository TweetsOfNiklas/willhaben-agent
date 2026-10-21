"""The one-time Vinted inbox exploration: read-only hidden tabs (inbox, then its newest conversation), Vinted's data
calls kept in a local capture file, once per version."""
import json
from types import SimpleNamespace

from app.db import get_setting
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


async def test_explore_once_reads_inbox_and_a_conversation(tmp_path, monkeypatch):
    sess = Sess(tmp_path)
    monkeypatch.setattr("app.willhaben.session.get_session", lambda: sess)
    await vinted_inbox.explore_once(delay_s=0)
    assert sess.opened == ["https://www.vinted.at/inbox", "https://www.vinted.at/inbox/12345678"]
    saved = json.loads(next(tmp_path.glob("*-vinted-inbox-explore.json")).read_text())
    assert saved["conversation_id"] == "12345678" and get_setting(vinted_inbox.SETTING) == vinted_inbox.VERSION
    await vinted_inbox.explore_once(delay_s=0)
    assert len(sess.opened) == 2                                   # once per version
