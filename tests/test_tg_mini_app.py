"""The Telegram Mini App (/tg): Telegram's own look, the same data as the dashboard, hits with their Geizhals rating
and the ad, the chats with sending, my ads, purchases and the search agents' switches."""
import pytest
from fastapi.testclient import TestClient

from app.db import Conversation, Draft, Hit, Message, MyAd, SearchAgent, session, utcnow
from app.services import chats


@pytest.fixture
def client():
    from app.main import app
    with TestClient(app) as c:
        yield c


def _data():
    with session() as db:
        a = SearchAgent(name="Raspberry Pi 5", source_url="https://www.willhaben.at/iad/x", webapi_url="/x")
        db.add(a)
        db.commit()
        db.refresh(a)
        h = Hit(agent_id=a.id, ad_id="555", heading="Raspberry Pi 5 8GB", url="https://www.willhaben.at/iad/object?adId=555",
                state="new", price=65, geizhals={"name": "Raspberry Pi 5 8GB", "url": "https://geizhals.at/a1",
                                                 "price_from": 89.0, "by": "geizhals"})
        db.add(h)
        db.add(Conversation(id="c1", partner_name="Erika", ad_title="Echo Show 8", role="seller", last_message_at=utcnow()))
        db.add(Message(conversation_id="c1", ext_id="m1", direction="in", text="Noch da?", created_at=utcnow()))
        db.add(Draft(conversation_id="c1", text="Ja, ist noch da.", alternatives=[{"label": "Bei 45 € bleiben",
                                                                                  "instruction": "Beim Preis bleiben."}]))
        db.add(MyAd(ad_id="900", heading="Fritzbox 7530", status="active"))
        db.commit()
        return a.id, h.id


def test_home_and_lists_in_telegrams_look(client):
    agent_id, hit_id = _data()
    home = client.get("/tg").text
    assert 'data-cell="hits"' in home and 'data-cell="chats"' in home and "Raspberry Pi 5" in home
    assert "web_app_setup_main_button" in home and "<html" in home and 'id="sidebar"' not in home   # no dashboard shell
    hits = client.get("/tg/hits").text
    assert f'data-hit="{hit_id}"' in hits and "data-geizhals" in hits and "89" in hits      # the Geizhals rating
    assert 'data-chat="c1"' in client.get("/tg/chats").text
    assert 'data-myad="900"' in client.get("/tg/myads").text
    client.post(f"/tg/agents/{agent_id}/toggle")
    with session() as db:
        assert db.get(SearchAgent, agent_id).enabled is False


def test_a_hit_opens_with_its_rating_and_is_saved_with_the_main_button(client, monkeypatch):
    from app import platforms
    _a, hit_id = _data()
    monkeypatch.setattr(platforms.get("willhaben"), "detail", lambda *a, **k: _none())
    page = client.get(f"/tg/hits/{hit_id}").text
    assert 'data-main-form="tg-save"' in page and "data-geizhals" in page and "data-geizhals-link" in page
    with session() as db:
        assert db.get(Hit, hit_id).state == "seen"                           # opened = seen
    client.post(f"/tg/hits/{hit_id}/state", data={"state": "saved"})
    with session() as db:
        assert db.get(Hit, hit_id).state == "saved"


async def _none():
    return None


def test_the_chat_sends_the_draft_as_edited(client, monkeypatch):
    _data()
    sent = []
    monkeypatch.setattr(chats, "enqueue_send", lambda did, text, now=False: sent.append((did, text, now)) or True)
    page = client.get("/tg/chats/c1").text
    assert 'data-main-form="tg-send"' in page and "Ja, ist noch da." in page and "Bei 45 € bleiben" in page
    with session() as db:
        draft_id = db.exec(__import__("sqlmodel").select(Draft.id)).one()
    client.post("/tg/chats/c1/send", data={"text": "Ja, noch da!", "draft_id": str(draft_id)})
    assert sent == [(draft_id, "Ja, noch da!", True)]
    client.post("/tg/chats/c1/send", data={"text": "Bis morgen"})              # no draft: my own text
    assert sent[-1][1:] == ("Bis morgen", True) and sent[-1][0] != draft_id


def test_telegram_buttons_open_the_mini_app(monkeypatch):
    from app.config import get_settings
    from app.tg_web import menu_url, tg_link
    monkeypatch.setattr(get_settings(), "public_url", "https://sh.example.org")
    assert tg_link("https://sh.example.org/chats/c1") == "https://sh.example.org/tg/chats/c1"
    assert tg_link("https://sh.example.org/hits?view=vinted:77") == "https://sh.example.org/tg/ad/vinted:77"
    assert tg_link("https://www.willhaben.at/iad/x") == "https://www.willhaben.at/iad/x"
    assert menu_url() == "https://sh.example.org/tg"
    monkeypatch.setattr(get_settings(), "public_url", "http://10.0.0.5:8000")
    assert menu_url() is None                                                    # Telegram opens https only



def test_the_mini_app_shows_what_the_site_shows(client, monkeypatch):
    """Close to the dashboard: the cards' Claude verdict, held-back reason and quick buttons, the viewer's price check
    and sync, the chat page's notes, instruction, manual send and actions, the list's role filter."""
    from app import platforms
    _a, hit_id = _data()
    with session() as db:
        h = db.get(Hit, hit_id)
        h.screen_relevant, h.screen_deal, h.screen_reason, h.buyer_fee = True, "good", "Gutes Angebot.", 2.45
        db.add(h)
        d = db.exec(__import__("sqlmodel").select(Draft)).one()
        d.needs_info = ["Abholzeit fehlt"]
        db.add(d)
        db.commit()
    hits = client.get("/tg/hits").text
    assert "data-claude" in hits and 'data-quick="saved"' in hits and "2,45" in hits
    assert ">Held back" in client.get("/tg/hits?state=filtered").text or "Held back" in client.get("/tg/hits").text
    monkeypatch.setattr(platforms.get("willhaben"), "detail", lambda *a, **k: _none())
    page = client.get(f"/tg/hits/{hit_id}").text
    assert "data-deal" in page and "data-sync" in page and "Gutes Angebot." in page
    client.post(f"/tg/hits/{hit_id}/quick", data={"state": "ignored", "tab": "new"})
    with session() as db:
        assert db.get(Hit, hit_id).state == "ignored"
    chat = client.get("/tg/chats/c1").text
    assert "data-notes" in chat and "Abholzeit fehlt" in chat and 'name="hint"' in chat and "data-chat-actions" in chat
    assert "/tg/chats/c1/mark-sent" in chat
    lst = client.get("/tg/chats?role=buyer").text
    assert 'data-chat="c1"' not in lst and 'data-chat="c1"' in client.get("/tg/chats?role=seller").text
    client.post("/tg/chats/c1/finished", data={"finished": "1"})
    with session() as db:
        assert db.get(Conversation, "c1").finished_at is not None


def test_the_refreshing_parts_do_not_pass_their_settings_to_the_links_inside(client, monkeypatch):
    """A link inside the chat list (hx-select="#tg-chats") opened the chat with only that part of it, which the chat
    page does not have: no messages, no composer, nothing refreshed."""
    import re
    _, hit_id = _data()
    monkeypatch.setattr(chats, "presence", lambda *a, **k: None, raising=False)
    pages = [client.get("/tg/chats").text, client.get("/tg/chats/c1").text]
    polled = [t for page in pages for t in re.findall(r"<div[^>]*hx-trigger=\"every[^>]*>", page)]
    assert polled and all('hx-disinherit="*"' in t for t in polled)
    hit = client.get(f"/tg/hits/{hit_id}").text
    assert "data-pricebar" in hit                       # the price bar from the Geizhals new price alone
