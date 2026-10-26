"""The weekly summary (services/digest): the last seven days in a few lines, sent on Mondays unless switched off."""
from datetime import timedelta

from app.db import Hit, Message, Purchase, SearchAgent, session, set_setting, utcnow
from app.services import digest


async def test_the_week_in_a_few_lines(monkeypatch):
    now = utcnow()
    with session() as db:
        a = SearchAgent(name="Raspberry", source_url="x", webapi_url="x")
        db.add(a)
        db.commit()
        db.refresh(a)
        for i in range(3):
            db.add(Hit(agent_id=a.id, ad_id=f"9{i}", heading="Pi", url="x", first_seen_at=now - timedelta(days=i)))
        db.add(Hit(agent_id=a.id, ad_id="old", heading="Pi", url="x", first_seen_at=now - timedelta(days=10)))
        db.add(Purchase(ad_id="1", status="paid", updated_at=now - timedelta(days=1)))
        db.add(Message(conversation_id="c", ext_id="m1", direction="in", text="Hallo", created_at=now - timedelta(hours=5)))
        db.commit()
    body = digest.lines(now)
    assert body[0] == "New hits: 3 (Raspberry 3)" and "Bought: 1 · open purchase orders: 0" in body
    assert "Messages received: 1" in body
    sent = []

    async def notify(kind, title, message, **kw):
        sent.append((kind, title, message))
    monkeypatch.setattr("app.services.notify.notify", notify)
    await digest.weekly_job()
    assert sent and sent[0][:2] == ("digest", "Your week")
    set_setting(digest.SETTING, "0")
    sent.clear()
    await digest.weekly_job()
    assert sent == []
