"""Activity list and inbox link to the app's own pages (ad viewer, chat, My ads), willhaben only when nothing here fits;
and the expand marker of "Comparable ads" in the price check."""
import re

from app.config import get_settings
from app.db import EventLog, log_event
from app.main import event_link
from app.services import notify as notify_mod
from app.services.my_ads import MYADVERTS_URL
from tests.test_dashboard import client  # noqa: F401  (client fixture)

WH_AD = "https://www.willhaben.at/iad/kaufen-und-verkaufen/d/raspberry-pi-4-4gb-1662146981/"


def test_event_link_prefers_own_pages():
    assert event_link(EventLog(kind="hit", message="x", url=WH_AD, ad_id="1662146981")) == \
        {"href": "/hits?view=1662146981", "ad": "1662146981", "external": False}   # opens the ad viewer
    assert event_link(EventLog(kind="message", message="x", url="https://www.willhaben.at/iad/myprofile/chat",
                               conversation_id="c1"))["href"] == "/chats/c1"
    assert event_link(EventLog(kind="ad_expiring", message="x", url=WH_AD, ad_id="1662146981"))["href"] == \
        "/myads?ad=1662146981"
    base = get_settings().public_url.rstrip("/")
    assert event_link(EventLog(kind="purchase", message="x", url=f"{base}/purchases/3")) == \
        {"href": "/purchases/3", "ad": None, "external": False}
    # older rows without ids: the ad id comes from the willhaben link
    assert event_link(EventLog(kind="price_drop", message="x", url=WH_AD))["ad"] == "1662146981"
    assert event_link(EventLog(kind="ad_renewed", message="x", url=WH_AD))["href"] == "/myads?ad=1662146981"
    # older rows without ids pointing to willhaben's generic chat / my-adverts pages
    assert event_link(EventLog(kind="message", message="x", url="https://www.willhaben.at/iad/myprofile/chat")) == \
        {"href": "/chats", "ad": None, "external": False}
    assert event_link(EventLog(kind="auto_reply", message="x", url="https://www.willhaben.at/iad/myprofile/chat/"))["href"] == "/chats"
    assert event_link(EventLog(kind="purchase", message="x", url="https://www.willhaben.at/iad/myprofile/chat/c7"))["href"] == \
        "/chats/c7"
    assert event_link(EventLog(kind="ad_expired", message="x", url=MYADVERTS_URL))["href"] == "/myads"
    # a bare app path (Claude token limit) stays in the app; "+N more new hits" (no link, no id) opens the hits page
    assert event_link(EventLog(kind="health", message="x", url="/settings/claude")) == \
        {"href": "/settings/claude", "ad": None, "external": False}
    assert event_link(EventLog(kind="hit", message="x")) == {"href": "/hits", "ad": None, "external": False}
    # nothing of ours fits: the external link stays, in a new tab
    assert event_link(EventLog(kind="meetup", message="x", url="https://calendar.google.com/e/1"))["external"] is True
    assert event_link(EventLog(kind="test", message="x")) is None


async def test_notify_stores_the_ids(monkeypatch):
    monkeypatch.setattr(notify_mod, "push_channel", lambda: "ha")
    monkeypatch.setattr("app.services.ha_auth.ha_url", lambda: "")
    await notify_mod.notify("message", "Message from Max", "Hallo", url="https://www.willhaben.at/iad/myprofile/chat",
                            data={"conversation_id": "c1", "ad_id": 1662146981})
    from app.db import recent_events
    e = recent_events(1)[0]
    assert e.conversation_id == "c1" and e.ad_id == "1662146981"


def test_dashboard_and_inbox_link_into_the_app(client):  # noqa: F811
    log_event("hit", "Raspberry: 40 €", WH_AD, ad_id="1662146981")
    log_event("message", "Message from Max: Hallo", "https://www.willhaben.at/iad/myprofile/chat", conversation_id="c1")
    log_event("ad_expiring", "Ad expires soon: Rad", WH_AD, ad_id="1662146981")
    page = client.get("/").text
    activity = page[page.index('id="activity"'):]
    activity = activity[:activity.index("</section>")]
    assert 'data-ad="1662146981"' in activity and 'href="/chats/c1"' in activity
    assert 'href="/myads?ad=1662146981"' in activity and 'target="_blank"' not in activity
    inbox = page[page.index('id="inbox-pop"'):]
    assert 'href="/chats/c1"' in inbox and 'data-ad="1662146981"' in inbox and 'href="/myads?ad=1662146981"' in inbox


def test_comparable_ads_have_an_expand_marker():
    from app.main import templates

    res = {"ad": {"price": 50.0}, "position": {"pos": None}, "comparables": {"count": 2, "closest": [
        {"ad_id": "1", "title": "A", "price": 40.0, "url": "u1"}, {"ad_id": "2", "title": "B", "price": 60.0, "url": "u2"}]}}
    html = templates.get_template("_deal.html").render(res=res, error=None, compact=True, h=None)
    summary = re.search(r"<summary.*?</summary>", html, re.S).group(0)
    assert "group-open:rotate-90" in summary and "list-none" in summary and "(2)" in summary


def test_expiring_soon_links_to_my_ads(client):  # noqa: F811
    from datetime import timedelta

    from app.db import MyAd, session, utcnow

    with session() as db:
        db.add(MyAd(ad_id="1662146981", heading="Rad", status="active", url=WH_AD, end_date=utcnow() + timedelta(days=2)))
        db.commit()
    page = client.get("/").text
    box = page[page.index("/myads?ad=1662146981") - 300:page.index("/myads?ad=1662146981") + 200]
    assert 'href="/myads?ad=1662146981"' in box and WH_AD not in box and 'target="_blank"' not in box


def test_view_deep_link_opens_the_ad_viewer(client):  # noqa: F811
    """Push link of a hit: /hits?view=<ad id>; the hits page takes it and the viewer script opens that ad."""
    page = client.get("/hits?view=1662146981")
    assert page.status_code == 200
    script = page.text[page.text.index('id="ad-viewer"'):]
    script = script[script.index("<script>"):script.index("</script>")]
    assert "searchParams.get('view')" in script and "/^\\d+$/.test(view)" in script
    assert "history.replaceState" in script and "searchParams.delete('view')" in script
