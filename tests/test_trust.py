"""The other person's account: its age and ratings where the site shows them (services/trust) – the willhaben profile
page and ad, Vinted's and eBay's item pages – shown in the ad viewer and the chat, warnings for young accounts and
accounts without ratings."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.db import Conversation, session
from app.services import chats, trust
from app.willhaben.parse import parse_detail
from app.willhaben.public import parse_member_profile

FIX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 26, tzinfo=timezone.utc)


def _profile(trust_signals, since="2009-06-09T00:00:00+0200"):
    page = {"sellerProfileWrapper": {"sellerProfile": {"registerDate": since, "location": "8530 Deutschlandsberg"}}}
    if trust_signals is not None:
        page["trustSignals"] = trust_signals
    return {"props": {"pageProps": page}}


def test_willhaben_profile_page():
    p = parse_member_profile(_profile({"replyTime": "Antwortet meistens innerhalb von 10 Std.",
                                       "reputation": {"feedback": {"overallScore": 1, "receivedCount": 9}}}))
    assert p["place"] == "8530 Deutschlandsberg" and p["since"].year == 2009 and p["ratings"] == 9
    assert p["score"] == 1.0 and p["reply_time"].startswith("Antwortet")
    assert parse_member_profile(_profile({}))["ratings"] == 0                  # trust signals, no rating: none yet
    assert parse_member_profile(_profile(None))["ratings"] is None             # no trust signals: unknown


def test_flags_and_facts():
    assert trust.flags(NOW - timedelta(days=12), 0, "willhaben", NOW) == [
        {"code": "young", "severity": "high", "label": "Account only 12 days old"},
        {"code": "no_ratings", "severity": "medium", "label": "No PayLivery ratings yet"}]
    assert [f["severity"] for f in trust.flags(NOW - timedelta(days=60), 5, "willhaben", NOW)] == ["medium"]
    assert trust.flags(NOW - timedelta(days=400), 3, "willhaben", NOW) == []
    assert trust.flags(None, None, "willhaben", NOW) == []                       # unknown: no warning
    assert trust.flags(None, 0, "vinted", NOW)[0]["label"] == "No ratings yet"
    assert trust.facts(datetime(2012, 7, 10), 9, "replies within 10 h") == ["member since 2012", "9 ratings",
                                                                           "replies within 10 h"]


def test_sites_parse_the_sellers_account():
    from app.platforms import ebay, vinted
    d = parse_detail(json.loads((FIX / "detail_1662146981_nextdata.json").read_text()))
    assert d.seller_since.year == 2012 and d.ad_uuid and d.seller_uuid
    v = vinted.parse_item((FIX / "vinted_item.rsc").read_text(), "10341874098")
    assert v.seller_ratings == 12
    e = ebay.parse_item_page((FIX / "ebay_item.html").read_text(), "920036165580")
    assert e.seller_since == datetime(2025, 7, 1, tzinfo=timezone.utc) and e.seller_ratings == 0


async def test_chat_reads_the_buyers_profile_once_a_week(monkeypatch):
    from app.willhaben import public
    read = []

    class Pub:
        async def member_profile(self, ad_uuid, user_uuid):
            read.append(user_uuid)
            return {"place": "1010 Wien", "since": datetime.now(timezone.utc) - timedelta(days=5), "ratings": 0,
                    "score": None, "reply_time": None}
    monkeypatch.setattr(public, "get_public", lambda: Pub())

    class Sess:
        presence = {}

        async def track_presence(self, uuid):
            pass
    monkeypatch.setattr(chats, "get_session", lambda: Sess())
    with session() as db:
        db.add(Conversation(id="t1", ad_id="1", partner_uuid="u1", ad_uuid="a1", partner_name="x"))
        db.commit()
    with session() as db:
        conv = db.get(Conversation, "t1")
    await chats.ask_presence(conv)
    with session() as db:
        conv = db.get(Conversation, "t1")
    assert conv.partner_place == "1010 Wien" and conv.partner_ratings == 0 and conv.partner_trust_at is not None
    tr = chats.partner_trust(conv)
    assert [f["code"] for f in tr["flags"]] == ["young", "no_ratings"]
    await chats.ask_presence(conv)
    assert read == ["u1"]                                                      # not again within the week

