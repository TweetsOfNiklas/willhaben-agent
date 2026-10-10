"""The 1b shell (app/static/ui.css + base.html): sidebar, badges, static files."""
from fastapi.testclient import TestClient

from app.db import Conversation, Hit, SearchAgent, session, utcnow
from app.main import app


def test_shell_and_static_files():
    with TestClient(app) as c:
        page = c.get("/chats").text
        assert 'class="rail"' in page and 'href="/static/ui.css"' in page and 'href="/static/legacy.css"' in page
        assert "picocss" not in page and "window.wbBuy" in page              # Pico gone; buy maths moved to the include
        assert '<main class="split">' in page                                 # ported: list → detail
        from pathlib import Path
        assert "page legacy" in Path("app/templates/myads_cleanup.html").read_text()   # not ported yet: transitional styles
        assert 'class="badge dot"' not in page                                # nothing to count
        for f in ("ui.css", "legacy.css"):
            r = c.get(f"/static/{f}")
            assert r.status_code == 200 and r.headers["content-type"].startswith("text/css")
        assert c.get("/static/nope.css").status_code == 404


def test_sidebar_badges_count_new_hits_and_unread_chats():
    with session() as db:
        db.add(SearchAgent(name="x", source_url="https://www.willhaben.at/iad/x", webapi_url="https://www.willhaben.at/webapi/x"))
        db.add(Conversation(id="a", unread_count=2, last_message_at=utcnow()))
        db.add(Conversation(id="b", unread_count=5, hidden_at=utcnow(), last_message_at=utcnow()))   # hidden: not counted
        db.commit()
        db.add(Hit(agent_id=1, ad_id="1", state="new", heading="t", url="https://www.willhaben.at/iad/object?adId=1"))
        db.commit()
    with TestClient(app) as c:
        page = c.get("/myads").text
    assert '<span class="badge dot">1</span>' in page and '<span class="badge dot">2</span>' in page


def test_credits_lists_github_contributors_without_the_author(monkeypatch):
    import httpx

    from app import main

    def handler(request):
        assert request.url.path == "/repos/lumascet/willhaben-agent/contributors"
        return httpx.Response(200, json=[
            {"login": "lumascet", "type": "User", "contributions": 85, "html_url": "https://github.com/lumascet", "avatar_url": ""},
            {"login": "TweetsOfNiklas", "type": "User", "contributions": 12, "html_url": "https://github.com/TweetsOfNiklas",
             "avatar_url": "https://avatars.githubusercontent.com/u/1?v=4"},
            {"login": "dependabot[bot]", "type": "Bot", "contributions": 3, "html_url": "", "avatar_url": ""}])

    real = httpx.AsyncClient
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setitem(main._contributors_cache, "list", None)
    with TestClient(app) as c:
        page = c.get("/credits").text
    special = page.split("Special thanks")[1].split("Built with")[0]
    assert "TweetsOfNiklas" in special and "12 commits" in special
    assert "lumascet" not in special and "dependabot" not in special
