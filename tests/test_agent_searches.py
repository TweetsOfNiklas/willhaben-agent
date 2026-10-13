"""Every search agent's settings drawer: several searches, and the sites it watches (search_agents.edit_searches)."""
import pytest
from fastapi.testclient import TestClient

from app import platforms
from app.db import SearchAgent, session
from app.services.search_agents import agent_platforms, agent_searches, create_agent, edit_searches, set_searches
from tests.fake_platform import FakePlatform
from tests.test_search_agents import URL

CATEGORY = "https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz/fahrraeder-radsport/fahrraeder-4552"


@pytest.fixture
def fake():
    p = FakePlatform()
    platforms.set_platform("fake", p)
    yield p
    platforms.reset()


@pytest.fixture
def client():
    from app.main import app
    with TestClient(app) as c:
        yield c


def _agent(agent_id):
    with session() as db:
        return db.get(SearchAgent, agent_id)


def _settled(url):
    """An agent whose first check is done (new hits would push)."""
    a = create_agent(url)
    with session() as db:
        a = db.get(SearchAgent, a.id)
        a.baseline_done = True
        db.add(a)
        db.commit()
        db.refresh(a)
    return a


def _edit(client, a, **data):
    form = {"name": a.name, "interval_min": str(a.interval_s // 60), **data}
    return client.post(f"/agents/{a.id}/edit", data=form, follow_redirects=False)


def test_a_plain_agent_gets_more_searches(client):
    a = _settled(URL)
    page = client.get("/agents").text
    assert 'name="add_keyword"' in page and 'name="drop_query"' not in page    # no search without a term
    assert page.count('name="drop_term"') == 1 and ' disabled aria-label="Remove: fahrrad"' in page   # the last one stays
    assert 'name="sites_shown"' not in page                                    # only willhaben is switched on
    r = _edit(client, a, add_keyword=" rennrad  carbon ", add_price_to="900")
    assert r.status_code == 303 and r.headers["location"] == "/agents"
    b = _agent(a.id)
    assert [(q["keyword"], q["price_to"]) for q in b.queries] == [("fahrrad", None), ("rennrad carbon", 900)]
    assert b.source_url == a.source_url and not b.baseline_done                # the new search starts quietly
    page = client.get("/agents").text
    assert page.count('name="drop_term"') == 2 and 'value="[&#34;rennrad carbon&#34;, null, 900.0]"' in page
    assert " disabled aria-label=" not in page                                 # two searches: either can go

    _edit(client, b, drop_term='["fahrrad", null, null]')                     # one left: a plain agent again
    c = _agent(a.id)
    assert c.queries is None and c.spec["keyword"] == "rennrad carbon" and "rennrad" in c.source_url
    assert [q["keyword"] for q in agent_searches(c)] == ["rennrad carbon"]


def test_sites_of_an_agent(client, fake):
    fake.min_interval_s = 900
    a = _settled(URL)
    page = client.get("/agents").text
    assert 'name="sites" value="fake"' in page and 'name="sites" value="willhaben" class="checkbox checkbox-primary checkbox-xs" checked' in page
    _edit(client, a, sites_shown="1", sites=["willhaben", "fake"])
    b = _agent(a.id)
    assert agent_platforms(b) == ["willhaben", "fake"] and not b.baseline_done
    assert [(q["platform"], q["keyword"]) for q in b.queries] == [("willhaben", "fahrrad"), ("fake", "fahrrad")]
    assert b.interval_s == 900                                                 # the slow site's floor

    _edit(client, b, sites_shown="1", sites=["fake"], add_keyword="gravel")   # untick willhaben, one more term
    c = _agent(a.id)
    assert [(q["platform"], q["keyword"]) for q in c.queries] == [("fake", "fahrrad"), ("fake", "gravel")]
    assert c.platform == "fake" and c.source_url == "https://fake.example/s/fahrrad"   # the main search moved

    r = _edit(client, c, sites_shown="1")                                      # no site at all: refused
    assert "error=" in r.headers["location"] and len(_agent(a.id).queries) == 2


def test_a_filter_search_needs_a_term_for_another_site(client, fake):
    a = _settled(CATEGORY)
    r = _edit(client, a, sites_shown="1", sites=["willhaben", "fake"])
    assert "error=" in r.headers["location"] and _agent(a.id).queries is None
    _edit(client, a, sites_shown="1", sites=["willhaben", "fake"], add_keyword="gravel")
    b = _agent(a.id)
    assert [(q["platform"], q["keyword"]) for q in b.queries] == [("willhaben", ""), ("willhaben", "gravel"),
                                                                    ("fake", "gravel")]


def test_edit_searches_rules(fake):
    a = create_agent(URL)
    assert edit_searches(a, add="fahrrad") is False                           # already there: nothing added
    assert edit_searches(a, sites=["willhaben", "fake", "fake"]) is True
    assert edit_searches(a, sites=["willhaben", "fake"], add="FAHRRAD ") is True   # another spelling is another search
    with pytest.raises(ValueError, match="at least one search"):
        edit_searches(a, drop=range(10))
    with pytest.raises(ValueError, match="Unknown platform"):
        edit_searches(a, sites=["nowhere"])
    platforms.set_platform("fake", None)
    with pytest.raises(ValueError, match="Unknown platform"):
        edit_searches(create_agent(URL), sites=["willhaben", "fake"])          # gone: cannot be chosen any more


async def test_the_first_check_after_adding_counts_as_seen(fake):
    from sqlmodel import select

    from app.db import Hit
    from app.services.search_agents import run_agent
    from tests.fake_platform import listing

    a = create_agent("https://fake.example/s/rad")
    fake.pages = [[listing("1")]]
    assert (await run_agent(a.id)).baseline                                    # the agent's own first check
    a = _agent(a.id)
    assert edit_searches(a, add="gravel")
    with session() as db:
        db.add(a)
        a.baseline_done = False                                                # as the route does
        db.commit()
    fake.pages = [[listing("1"), listing("2", "Gravelbike")]]
    res = await run_agent(a.id)
    assert res.baseline and res.new == []
    with session() as db:
        assert {h.ad_id: h.state for h in db.exec(select(Hit).where(Hit.agent_id == a.id))} == {"1": "seen", "2": "seen"}
    fake.pages = [[listing("3", "Gravel neu")]]
    assert [h.ad_id for h in (await run_agent(a.id)).new] == ["3"]             # afterwards new ads push again


def test_an_agent_from_before_the_platforms_keeps_its_term(client, fake):
    """Old rows have no `spec`: the search term is read back from the stored link (seen on a real install)."""
    with session() as db:
        a = SearchAgent(name="Echo Show 8", platform="willhaben", source_url=URL + "+neu&PRICE_TO=80",
                        webapi_url="https://www.willhaben.at/webapi/iad/search/atz/seo/kaufen-und-verkaufen/marktplatz"
                                   "?keyword=fahrrad+neu&PRICE_TO=80&rows=30", spec=None, baseline_done=True)
        db.add(a)
        db.commit()
        db.refresh(a)
    q = agent_searches(a)[0]
    assert (q["keyword"], q["price_to"], q["source_url"], q["webapi_url"]) == ("fahrrad neu", 80, a.source_url, a.webapi_url)
    r = _edit(client, a, sites_shown="1", sites=["willhaben", "fake"])
    assert r.headers["location"] == "/agents"
    b = _agent(a.id)
    assert [(q["platform"], q["keyword"], q["price_to"]) for q in b.queries] == [("willhaben", "fahrrad neu", 80),
                                                                                 ("fake", "fahrrad neu", 80)]
    assert b.webapi_url == a.webapi_url                                        # the old search runs unchanged


def test_a_search_term_is_one_row_for_every_site(client, fake):
    a = _settled(URL)
    _edit(client, a, sites_shown="1", sites=["willhaben", "fake"], add_keyword=["gravel", "rennrad"],
          add_price_from=["", "100"], add_price_to=["", "900"])                  # two added rows, one Save
    b = _agent(a.id)
    assert [(q["platform"], q["keyword"], q["price_from"], q["price_to"]) for q in b.queries] == [
        ("willhaben", "fahrrad", None, None), ("willhaben", "gravel", None, None), ("willhaben", "rennrad", 100, 900),
        ("fake", "fahrrad", None, None), ("fake", "gravel", None, None), ("fake", "rennrad", 100, 900)]
    page = client.get("/agents").text
    assert page.count('name="drop_term"') == 3 and "6 searches per check" in page   # a term once, not once per site
    _edit(client, b, sites_shown="1", sites=["willhaben", "fake"], drop_term='["gravel", null, null]')
    assert [(q["platform"], q["keyword"]) for q in _agent(a.id).queries] == [
        ("willhaben", "fahrrad"), ("willhaben", "rennrad"), ("fake", "fahrrad"), ("fake", "rennrad")]


def test_the_only_term_is_replaced_in_one_save(client):
    a = _settled(URL)
    r = _edit(client, a, drop_term='["fahrrad", null, null]', add_keyword="gravel")
    assert r.headers["location"] == "/agents"
    b = _agent(a.id)
    assert b.queries is None and b.spec["keyword"] == "gravel" and not b.baseline_done
    r = _edit(client, b, drop_term='["gravel", null, null]')                     # nothing left: refused
    assert "error=" in r.headers["location"] and _agent(a.id).spec["keyword"] == "gravel"


def test_a_search_without_a_term_is_named_in_words(client, fake):
    a = _settled(CATEGORY)
    page = client.get("/agents").text
    assert ' disabled aria-label="Remove: Fahrraeder"' in page and "only willhaben" in page and ">search</a>" not in page
    _edit(client, a, sites_shown="1", sites=["willhaben", "fake"], add_keyword="gravel")
    page = client.get("/agents").text
    assert 'name="drop_query" value="0"' in page and page.count('name="drop_term"') == 1
    assert "3 searches per check" in page


def test_save_puts_every_term_on_every_site(client, fake):
    a = _settled(URL)
    edit_searches(a, sites=["willhaben", "fake"], add="gravel")
    set_searches(a, [q for q in agent_searches(a) if (q["platform"], q["keyword"]) != ("fake", "gravel")])  # a row from before
    with session() as db:
        db.add(a)
        db.commit()
    page = client.get("/agents").text
    assert "not on Fake yet" in page and page.count('name="drop_term"') == 2 and "4 searches per check" in page
    _edit(client, _agent(a.id), sites_shown="1", sites=["willhaben", "fake"])     # any Save fills it in
    assert sorted((q["platform"], q["keyword"]) for q in _agent(a.id).queries) == [
        ("fake", "fahrrad"), ("fake", "gravel"), ("willhaben", "fahrrad"), ("willhaben", "gravel")]


def test_a_watched_site_that_is_gone_keeps_what_it_has(fake):
    a = create_agent(URL)
    edit_searches(a, sites=["willhaben", "fake"])
    platforms.set_platform("fake", None)                                        # gone (or switched off)
    assert edit_searches(a, sites=["willhaben", "fake"], add="gravel") is True   # no error; it just gets nothing new
    assert [(q["platform"], q["keyword"]) for q in a.queries] == [("willhaben", "fahrrad"), ("fake", "fahrrad"),
                                                                  ("willhaben", "gravel")]


def test_a_stale_or_made_up_term_key_is_ignored():
    from app.services.search_agents import parse_term_key, term_key

    assert parse_term_key(term_key(("Spüler", 100.0, None))) == ("Spüler", 100.0, None)
    for raw in ("", "x", "[1, 2]", '["", null, null]', '["a", "1", null]', '["a", true, null]', '{"a": 1}'):
        assert parse_term_key(raw) is None
