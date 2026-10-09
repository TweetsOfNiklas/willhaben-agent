"""Predefined screening reasons: stored per hit, chosen per agent, shown as badges, in the HA event."""
from app import db as dbmod
from app.services import screening
from app.services.search_agents import _screen_data, create_agent
from tests.test_dashboard import client  # noqa: F401  (pytest fixture)
from tests.test_search_agents import URL


def _agent(**kw):
    return dbmod.SearchAgent(**{"name": "x", "source_url": "u", "webapi_url": "u", "screen": True,
                                "screen_min": "match", **kw})


def _hit(relevant, deal="fair", issues=None, private=True):
    return dbmod.Hit(agent_id=1, ad_id="1", heading="x", url="u", screen_relevant=relevant, screen_deal=deal,
                     screen_issues=issues, is_private=private)


def test_passes_by_chosen_reasons():
    accessory = _hit(False, "unknown", ["accessory_only"])
    assert not screening.passes(_agent(), accessory)                       # hidden by default
    assert screening.passes(_agent(screen_hide="wrong_product"), accessory)  # the user allows accessories
    dealer = _hit(True, "good", ["dealer"], private=False)
    assert screening.passes(_agent(), dealer)                              # dealers not hidden by default
    assert not screening.passes(_agent(screen_hide="dealer"), dealer)
    scam = _hit(True, "great", ["scam_suspect"])
    assert not screening.passes(_agent(), scam)                            # never a "top deal" push for a scam
    assert not screening.passes(_agent(screen_hide="-"), _hit(False, "unknown", []))   # not the item, no reason
    assert not screening.passes(_agent(screen_hide="-"), _hit(False))                  # old hit without reasons
    pricey = _hit(True, "overpriced", ["too_expensive"])
    assert screening.passes(_agent(screen_hide="too_expensive"), pricey)   # price is left to the threshold
    assert not screening.passes(_agent(screen_min="fair"), pricey)


def test_labels_and_event_data():
    assert screening.verdict_labels(_hit(False, issues=["accessory_only", "dealer"])) == [
        "accessory / spare part only", "dealer"]
    assert screening.verdict_labels(_hit(True, "good", ["too_expensive", "defective"])) == [
        "good price", "defective / damaged"]
    assert screening.verdict_labels(_hit(False)) == ["wrong item"]
    assert "accessory / spare part only" in screening.summary(_hit(False, issues=["accessory_only"]))
    assert _screen_data(_hit(False, issues=["bundle_only"]))["screen_issues"] == ["bundle_only"]


async def test_screen_stores_reasons_and_dealer_from_willhaben(monkeypatch):
    from app.llm.base import LLMResult

    class Fake:
        model = "fake"
        schema = None

        async def complete(self, *, system, prompt, schema, images=None):
            Fake.schema, Fake.prompt = schema, prompt
            return LLMResult(data={"results": [
                {"nr": 1, "relevant": False, "deal": "unknown", "issues": ["accessory_only", "bogus"], "reason": "Nur Schlauch"},
                {"nr": 2, "relevant": True, "deal": "good", "issues": [], "reason": "passt"}]}, model="fake")

    monkeypatch.setattr(screening, "get_backend", lambda *a: Fake())
    agent = create_agent(URL)
    with dbmod.session() as db:
        a = db.get(dbmod.SearchAgent, agent.id)
        a.screen = True
        db.add(a)
        hits = [dbmod.Hit(agent_id=agent.id, ad_id="1000000001", heading="Schlauch", url="u", is_private=True),
                dbmod.Hit(agent_id=agent.id, ad_id="1000000002", heading="Rad", url="u", is_private=False)]
        for h in hits:
            db.add(h)
        db.commit()
        for h in hits:
            db.refresh(h)
    assert await screening.screen(agent.id, hits) == 2
    assert hits[0].screen_issues == ["accessory_only"] and hits[1].screen_issues == ["dealer"]
    assert "issues" in Fake.schema["properties"]["results"]["items"]["required"]


def test_agent_form_saves_hidden_reasons(client):  # noqa: F811
    agent = create_agent(URL)
    page = client.get("/agents").text
    assert 'name="screen_hide" value="accessory_only" checked' in page and 'value="dealer" checked' not in page
    client.post(f"/agents/{agent.id}/edit", data={"name": "Rad", "interval_min": "5", "screen_on": "on",
                                                  "screen_hide": ["dealer", "defective", "too_expensive"]})
    with dbmod.session() as db:
        a = db.get(dbmod.SearchAgent, agent.id)
    assert a.screen_hide == "defective,dealer"                             # price reasons are not hideable
    client.post(f"/agents/{agent.id}/edit", data={"name": "Rad", "interval_min": "5", "screen_on": "on"})
    with dbmod.session() as db:
        assert db.get(dbmod.SearchAgent, agent.id).screen_hide == "-"      # nothing ticked = hide nothing
