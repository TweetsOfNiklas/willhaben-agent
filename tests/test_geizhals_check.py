"""Claude checks the Geizhals product of a screening agent's new hits and looks for the right one (switch "Check the
Geizhals product")."""
import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import Hit, SearchAgent, session
from app.llm.base import LLMError, LLMResult
from app.services import deals, geizhals, geizhals_check
from app.services.geizhals import Hit as GHit

AUTO_PRO_24 = {"name": "Ubiquiti UniFiSwitch Pro 24 Rackmount Gigabit Managed Switch, 24x RJ-45, 2x SFP+, 400W PoE++",
               "url": "https://geizhals.at/ubiquiti-unifiswitch-pro-24-usw-pro-24-poe-a2194138.html", "price_from": 739.88}
RIGHT_24 = GHit(name="Ubiquiti UniFiSwitch 24 Rackmount Gigabit Managed Switch, 24x RJ-45, 2x SFP, 95W PoE+",
                url="https://geizhals.at/ubiquiti-unifiswitch-24-usw-24-poe-a2189707.html", price=406.87)


class Script:
    model = "fake-haiku"

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    async def complete(self, *, system, prompt, schema, images=None):
        self.calls.append({"system": system, "prompt": prompt})
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return LLMResult(data=a, model=self.model)


@pytest.fixture
def agent():
    with session() as db:
        a = SearchAgent(name="Unifi", source_url="https://www.willhaben.at/iad/x", webapi_url="x", screen=True,
                        geizhals_check=True)
        db.add(a)
        db.commit()
        db.refresh(a)
        return a


def _hit(agent, n, heading, gh, price=200.0):
    with session() as db:
        h = Hit(agent_id=agent.id, ad_id=str(n), heading=heading, url=f"u{n}", price=price, geizhals=gh)
        db.add(h)
        db.commit()
        db.refresh(h)
        return h


def _stored(h):
    with session() as db:
        return db.get(Hit, h.id).geizhals


async def test_right_product_is_kept_wrong_one_replaced(agent, monkeypatch):
    ok = _hit(agent, 1, "Raspberry Pi 4 4GB", {"name": "Raspberry Pi 4 Modell B, 4GB RAM", "url": "https://geizhals.at/p-a1.html",
                                               "price_from": 114.95})
    wrong = _hit(agent, 2, "Ubiquiti UniFiSwitch 24 PoE USW-24-POE", dict(AUTO_PRO_24))
    searched = []

    async def candidates(queries):
        searched.append(queries)
        return [GHit(name=AUTO_PRO_24["name"], url=AUTO_PRO_24["url"], price=739.88), RIGHT_24]

    monkeypatch.setattr(deals, "_gh_candidates", candidates)
    fake = Script({"results": [{"nr": 1, "ok": True, "product": "", "queries": []},
                               {"nr": 2, "ok": False, "product": "Ubiquiti UniFi Switch 24 PoE (USW-24-POE)",
                                "queries": ["USW-24-POE", "UniFi Switch 24 PoE"]}]},
                  {"results": [{"nr": 2, "pick": 2}]})
    monkeypatch.setattr(geizhals_check, "get_backend", lambda *a: fake)
    assert await geizhals_check.check(agent, [ok, wrong]) == 2
    assert "Geizhals: Ubiquiti UniFiSwitch Pro 24" in fake.calls[0]["prompt"] and "(neu ab 739.88 €)" in fake.calls[0]["prompt"]
    assert searched == [["USW-24-POE", "UniFi Switch 24 PoE"]]
    assert "2. Ubiquiti UniFiSwitch 24 Rackmount" in fake.calls[1]["prompt"] and "ab 406.87 €" in fake.calls[1]["prompt"]
    assert _stored(ok) == {"name": "Raspberry Pi 4 Modell B, 4GB RAM", "url": "https://geizhals.at/p-a1.html",
                           "price_from": 114.95, "checked": True}
    assert _stored(wrong) == {"name": RIGHT_24.name, "url": RIGHT_24.url, "price_from": 406.87, "by": "claude",
                              "checked": True}
    assert wrong.geizhals["price_from"] == 406.87                     # the caller's object too (the rule uses it)

    assert await geizhals_check.check(agent, [ok, wrong]) == 0        # checked once: a price drop keeps the product
    assert len(fake.calls) == 2


async def test_no_product_no_price_and_failures_change_nothing(agent, monkeypatch):
    junk = _hit(agent, 3, "Konvolut Netzwerkkabel", {})                      # nothing found, nothing to find
    wrong = _hit(agent, 4, "UniFi Switch 8 150W", {"name": "Ubiquiti UniFiSwitch Lite 8", "url": "https://geizhals.at/l-a2.html",
                                                    "price_from": 120.0})
    failed = _hit(agent, 5, "Raspberry Pi 5", None)                          # lookup failed: asked again later

    async def nothing(queries):
        return []

    monkeypatch.setattr(deals, "_gh_candidates", nothing)
    fake = Script({"results": [{"nr": 1, "ok": False, "product": "", "queries": []},
                               {"nr": 2, "ok": False, "product": "Ubiquiti UniFi Switch 8 150W", "queries": ["US-8-150W"]}]})
    monkeypatch.setattr(geizhals_check, "get_backend", lambda *a: fake)
    await geizhals_check.check(agent, [junk, wrong, failed])
    assert _stored(junk) == {"checked": True} and _stored(wrong) == {"checked": True}   # the wrong price is gone
    assert _stored(failed) is None and len(fake.calls) == 1                  # no results: no pick call

    other = _hit(agent, 6, "iPhone 16 Pro", {"name": "Apple iPhone 16 128GB", "url": "https://geizhals.at/i-a3.html",
                                             "price_from": 842.0})
    monkeypatch.setattr(geizhals_check, "get_backend", lambda *a: Script(LLMError("limit")))
    assert await geizhals_check.check(agent, [other]) == 0
    assert _stored(other) == {"name": "Apple iPhone 16 128GB", "url": "https://geizhals.at/i-a3.html", "price_from": 842.0}


async def test_searches_are_capped_and_stop_when_geizhals_blocks(agent, monkeypatch):
    hits = [_hit(agent, 10 + i, f"Produkt {i}", {}) for i in range(5)]
    calls = []

    async def blocked(queries):
        calls.append(queries)
        raise geizhals.GeizhalsError("Geizhals answered HTTP 429")

    monkeypatch.setattr(deals, "_gh_candidates", blocked)
    verdicts = [{"nr": i + 1, "ok": False, "product": f"Produkt {i}", "queries": [f"Produkt {i}"]} for i in range(5)]
    monkeypatch.setattr(geizhals_check, "get_backend", lambda *a: Script({"results": verdicts}))
    await geizhals_check.check(agent, hits)
    assert calls == [["Produkt 0"]]                                          # stopped at the first block
    assert all(_stored(h) == {"checked": True} for h in hits)
    assert geizhals_check.MAX_SEARCHED == 3


def test_drawer_switch_saves_only_with_screening(agent):
    from app.main import app

    with TestClient(app) as c:
        page = c.get("/agents").text
        assert 'name="gh_check"' in page and "Check the Geizhals product" in page and "checks the Geizhals product" in page
        form = {"name": "Unifi", "interval_min": "10", "screen_on": "1", "screen_min": "match"}
        c.post(f"/agents/{agent.id}/edit", data={**form, "gh_check": "on"}, follow_redirects=False)
        with session() as db:
            assert db.get(SearchAgent, agent.id).geizhals_check is True
        c.post(f"/agents/{agent.id}/edit", data=form, follow_redirects=False)              # switched off
        with session() as db:
            assert db.get(SearchAgent, agent.id).geizhals_check is False
        c.post(f"/agents/{agent.id}/edit", data={"name": "Unifi", "interval_min": "10", "gh_check": "1"},
               follow_redirects=False)                                                       # no screening: no check
        with session() as db:
            assert db.get(SearchAgent, agent.id).geizhals_check is False


async def test_agent_run_checks_before_the_rule(monkeypatch):
    """A screening agent with the switch and the 30 % rule: the automatic product (the dear Pro 24, 740 €) would let a
    350 € ad through as 53 % below; Claude's product (407 €) holds it back. Without the switch nothing is checked."""
    from app import platforms
    from app.services import search_agents
    from tests.fake_platform import FakePlatform, listing

    fake_site = FakePlatform()
    platforms.set_platform("fake", fake_site)
    try:
        async def match(query, **kw):
            return dict(AUTO_PRO_24)

        async def candidates(queries):
            return [RIGHT_24]

        async def notify(*a, **kw):
            return True

        async def screen(agent_id, hits):
            return 0

        monkeypatch.setattr(deals, "geizhals_match", match)
        monkeypatch.setattr(deals, "_gh_candidates", candidates)
        monkeypatch.setattr(search_agents, "notify", notify)
        monkeypatch.setattr(search_agents.screening, "screen", screen)
        fake = Script({"results": [{"nr": 1, "ok": False, "product": "Ubiquiti UniFi Switch 24 PoE",
                                    "queries": ["USW-24-POE"]}]},
                      {"results": [{"nr": 1, "pick": 1}]})
        monkeypatch.setattr(geizhals_check, "get_backend", lambda *a: fake)
        a = search_agents.create_agent("https://fake.example/s/unifi")
        fake_site.pages = [[]]
        await search_agents.run_agent(a.id)                                   # baseline
        with session() as db:
            row = db.get(SearchAgent, a.id)
            row.screen, row.geizhals_check, row.geizhals_pct = True, True, 30
            db.add(row)
            db.commit()
        fake_site.pages = [[listing("1", "Ubiquiti UniFiSwitch 24 PoE USW-24-POE", 350.0)]]
        await search_agents.run_agent(a.id)
        with session() as db:
            h = db.exec(select(Hit).where(Hit.agent_id == a.id)).one()
        assert h.geizhals["by"] == "claude" and h.geizhals["price_from"] == 406.87
        assert h.state == "filtered" and len(fake.calls) == 2              # 350 € is only 14 % below 407 €

        with session() as db:
            row = db.get(SearchAgent, a.id)
            row.geizhals_check = False
            db.add(row)
            db.commit()
        fake_site.pages = [[listing("1", "Ubiquiti UniFiSwitch 24 PoE USW-24-POE", 350.0),
                            listing("2", "Ubiquiti UniFiSwitch 24 PoE", 340.0)]]
        await search_agents.run_agent(a.id)
        assert len(fake.calls) == 2                                          # switched off: no check
    finally:
        platforms.reset()
