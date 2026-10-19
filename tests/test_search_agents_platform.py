"""The agent pipeline works for any platform adapter: no willhaben in it (checked with a fake site)."""
import httpx
import pytest
from sqlmodel import select

from app import platforms
from app.config import get_settings
from app.db import Hit, SearchAgent, session
from app.platforms import Blocked, InvalidSearchUrl, PlatformError
from app.services import notify as notify_mod
from app.services import scam
from app.services.search_agents import agent_spec, create_agent, run_agent
from tests.fake_platform import FakePlatform, listing


@pytest.fixture
def fake():
    p = FakePlatform()
    platforms.set_platform("fake", p)
    yield p
    platforms.reset()


@pytest.fixture
def ha(monkeypatch):
    import json

    sent: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"message": "ok"})

    s = get_settings()
    monkeypatch.setattr(s, "ha_url", "http://ha.local:8123")
    monkeypatch.setattr(s, "ha_token", "t")
    monkeypatch.setattr(notify_mod, "_transport", httpx.MockTransport(handler))
    return sent


def _hits(agent_id: int) -> dict[str, Hit]:
    with session() as db:
        return {h.ad_id: h for h in db.exec(select(Hit).where(Hit.agent_id == agent_id))}


def test_create_agent_on_the_fake_site(fake):
    a = create_agent("https://fake.example/s/rennrad?page=2")
    assert a.platform == "fake" and a.name == "rennrad" and a.source_url == "https://fake.example/s/rennrad"
    assert a.spec == {"platform": "fake", "url": "https://fake.example/s/rennrad", "keyword": "rennrad", "sort": "newest"}
    assert agent_spec(a).keyword == "rennrad" and a.webapi_url == a.source_url
    with pytest.raises(InvalidSearchUrl):
        create_agent("https://fake.example/ad/1")
    with pytest.raises(InvalidSearchUrl):
        create_agent("https://example.org/s/x")


def test_legacy_rows_are_willhaben_searches():
    with session() as db:
        a = SearchAgent(name="old", source_url="https://www.willhaben.at/iad/x?keyword=rad", webapi_url="https://www.willhaben.at/webapi/x")
        db.add(a)
        db.commit()
        db.refresh(a)
    spec = agent_spec(a)
    assert spec.platform == "willhaben" and spec.api == "https://www.willhaben.at/webapi/x" and spec.url == a.source_url


async def test_baseline_new_hit_price_drop_and_no_bumps(fake, ha):
    fake.pages = [[listing("1", "Rennrad", 400.0), listing("2", "Rad", 90.0)]]
    a = create_agent("https://fake.example/s/rennrad")
    res = await run_agent(a.id)
    assert res.baseline and not res.new and {h.state for h in _hits(a.id).values()} == {"seen"}
    assert all(h.platform == "fake" for h in _hits(a.id).values()) and not ha
    fake.pages = [[listing("1", "Rennrad", 350.0, is_bumped=None), listing("3", "Neues Rad", 120.0)]]
    res = await run_agent(a.id)
    assert [h.ad_id for h in res.new] == ["3"] and [h.ad_id for h in res.price_drops] == ["1"] and res.bumped == 0
    hits = _hits(a.id)
    assert hits["3"].state == "new" and hits["1"].old_price == 400.0 and hits["1"].price == 350.0
    kinds = [p.rsplit("/", 1)[1] for p, _ in ha]
    assert kinds == ["willhaben_hit", "willhaben_price_drop"]        # event names stay, the payload says the site
    hit_payload, drop_payload = ha[0][1], ha[1][1]
    assert hit_payload["platform"] == "fake" and hit_payload["ref"] == "fake:3" and hit_payload["ad_id"] == "3"
    assert drop_payload["ref"] == "fake:1" and "350 €" in drop_payload["message"]
    assert fake.searched[-1].keyword == "rennrad"


async def test_errors_blocks_and_switched_off(fake):
    a = create_agent("https://fake.example/s/x")
    fake.pages = [PlatformError("fake is down")]
    res = await run_agent(a.id)
    with session() as db:
        row = db.get(SearchAgent, a.id)
    assert res.error == "fake is down" and row.last_error == "fake is down" and row.last_ok_at is None
    fake.pages = [Blocked("fake: pause")]
    assert "pause" in (await run_agent(a.id)).error
    platforms.set_platform("fake", None)                           # the site vanished from the registry
    assert "Unknown platform" in (await run_agent(a.id)).error


async def test_multi_query_agent_runs_each_query_on_its_platform(fake):
    from app.db import ProductSearch
    from app.services.search_agents import create_agent_from_search

    with session() as db:
        ps = ProductSearch(description="Rennrad bis 500", status="done",
                           plan={"summary": "Rennrad", "max_price": 500, "queries": [
                               {"keyword": "rennrad", "price_from": None, "why": "x"},
                               {"keyword": "road bike", "price_from": 50, "category_path": "kaufen-und-verkaufen/sport", "why": "y"}]})
        db.add(ps)
        db.commit()
        db.refresh(ps)
    a = create_agent_from_search(ps.id, platform="fake", screen=False)
    assert a.platform == "fake" and [q["platform"] for q in a.queries] == ["fake", "fake"]
    assert a.queries[1]["spec"]["price_from"] == 50 and "category" not in a.queries[1]["spec"]   # categories are willhaben's
    fake.pages = [[listing("1")], [listing("1"), listing("2", "Road bike", 300.0)]]
    res = await run_agent(a.id)
    assert res.baseline and set(_hits(a.id)) == {"1", "2"} and [s.keyword for s in fake.searched] == ["rennrad", "road bike"]
    nokw = FakePlatform()
    nokw.capabilities = frozenset({"search_url"})
    platforms.set_platform("nokw", nokw)
    with pytest.raises(ValueError, match="keyword search"):
        create_agent_from_search(ps.id, platform="nokw", screen=False)


def test_scam_rules_follow_the_sites_country():
    de = {"heading": "Rad", "body": "Überweisung an IBAN DE89 3704 0044 0532 0130 00 bitte", "price": 100, "is_private": True}
    assert [f["code"] for f in scam.flags(de, home_country="DE")] == ["iban"]
    assert [f["code"] for f in scam.flags(de, home_country="AT")] == ["foreign_iban"]
    assert scam.flags(de, home_country="AT")[0]["severity"] == "warn"
    nl = {**de, "body": "IBAN NL91 ABNA 0417 1643 00"}
    assert scam.flags(nl, home_country="DE")[0]["severity"] == "warn" and scam.flags(nl, home_country="AT")[0]["severity"] == "high"
    off = {"heading": "Rad", "body": "Keine Kleinanzeigen-Nachrichten, nur WhatsApp", "price": 100, "is_private": True}
    assert "off_platform" in [f["code"] for f in scam.flags(off, home_country="DE")]
    assert scam.home_country_of("willhaben") == "AT" and scam.home_country_of("nope") == "AT"
    safe = {"heading": "Rad", "body": "Nur Vorkasse, sicher bezahlen möglich", "price": 100, "is_private": True}
    assert "prepay_only" not in [f["code"] for f in scam.flags(safe, home_country="DE")]


async def test_negotiable_kleinanzeigen_ads_are_never_new(fake, ha, monkeypatch):
    """Kleinanzeigen "VB" ads are left out of the hits page; they never count or push as new either (the sidebar
    counted them while the page showed nothing). Ones stored before move once at startup. (The fake site stands in
    for Kleinanzeigen: its search gives every listing its own id.)"""
    from app.services import search_agents
    monkeypatch.setattr(search_agents, "NEGOTIABLE_HIDDEN", ("fake",))
    fake.pages = [[]]
    a = create_agent("https://fake.example/s/rad")
    await run_agent(a.id)                                            # first check: nothing yet
    fake.pages = [[listing("1", "Rad", None, price_display="VB"),
                   listing("2", "Rad", 120.0, price_display="120 € VB"), listing("3", "Rad", 90.0)]]
    res = await run_agent(a.id)
    hits = _hits(a.id)
    assert {k: h.state for k, h in hits.items()} == {"1": "filtered", "2": "filtered", "3": "new"}
    assert [h.ad_id for h in res.new] == ["3"] and len(ha) == 1                 # one push, for the priced ad
    with session() as db:
        db.add(Hit(agent_id=a.id, platform="fake", ad_id="4", heading="Alt", url="u", price_display="VB",
                   state="new"))
        db.commit()
    assert search_agents.refile_negotiable() == 1 and search_agents.refile_negotiable() == 0
    assert _hits(a.id)["4"].state == "filtered"
