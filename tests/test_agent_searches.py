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
    assert b.source_url == a.source_url and b.baseline_done                    # only the new search starts quietly
    assert [q.get("quiet") for q in b.queries] == [None, True]
    page = client.get("/agents").text
    assert page.count('name="drop_term"') == 2 and 'value="[&#34;rennrad carbon&#34;, null, 900.0]"' in page
    assert " disabled aria-label=" not in page                                 # two searches: either can go

    _edit(client, b, drop_term='["fahrrad", null, null]')                     # one left: a plain agent again
    c = _agent(a.id)
    assert c.queries is None and c.spec["keyword"] == "rennrad carbon" and "rennrad" in c.source_url
    assert not c.baseline_done                                                 # its first check is still to come
    assert [q["keyword"] for q in agent_searches(c)] == ["rennrad carbon"]


def test_sites_of_an_agent(client, fake):
    fake.min_interval_s = 900
    a = _settled(URL)
    page = client.get("/agents").text
    assert 'name="sites" value="fake"' in page and 'name="sites" value="willhaben" class="checkbox checkbox-primary checkbox-xs" checked' in page
    _edit(client, a, sites_shown="1", sites=["willhaben", "fake"])
    b = _agent(a.id)
    assert agent_platforms(b) == ["willhaben", "fake"] and b.baseline_done
    assert [q.get("quiet") for q in b.queries] == [None, True]                 # quiet: the new site's search
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
        db.commit()
    fake.pages = [[listing("1")], [listing("1"), listing("2", "Gravelbike")]]   # rad, then gravel
    res = await run_agent(a.id)
    assert not res.baseline and res.new == []                                  # gravel's first check
    with session() as db:
        assert {h.ad_id: h.state for h in db.exec(select(Hit).where(Hit.agent_id == a.id))} == {"1": "seen", "2": "seen"}
    assert [q.get("quiet") for q in _agent(a.id).queries] == [None, None]
    fake.pages = [[], [listing("3", "Gravel neu")]]
    assert [h.ad_id for h in (await run_agent(a.id)).new] == ["3"]             # afterwards its new ads push


def _hit_states(agent_id):
    from sqlmodel import select

    from app.db import Hit

    with session() as db:
        return {h.ad_id: h.state for h in db.exec(select(Hit).where(Hit.agent_id == agent_id))}


def _save_during_the_check(monkeypatch, fake, agent_id, **edit):
    """The drawer is saved (edit_searches) while the next check's first search runs."""
    search = fake.search

    async def save_meanwhile(spec, **kw):
        monkeypatch.setattr(fake, "search", search)
        with session() as db:
            row = db.get(SearchAgent, agent_id)
            edit_searches(row, **edit)
            db.add(row)
            db.commit()
        return await search(spec, **kw)

    monkeypatch.setattr(fake, "search", save_meanwhile)


async def test_adding_a_search_mutes_only_that_search(client, fake):
    """The searches the agent had keep pushing in the check after a term was added (it once muted the whole agent)."""
    from app.services.search_agents import run_agent
    from tests.fake_platform import listing

    a = create_agent("https://fake.example/s/rad")
    fake.pages = [[listing("1")]]
    await run_agent(a.id)
    _edit(client, _agent(a.id), add_keyword="gravel")
    fake.pages = [[listing("1"), listing("4", "Rad neu")], [listing("4", "Rad neu"), listing("2", "Gravelbike")]]
    res = await run_agent(a.id)
    assert [h.ad_id for h in res.new] == ["4"]                                 # rad's, though gravel finds it too
    assert _hit_states(a.id) == {"1": "seen", "4": "new", "2": "seen"}


async def test_a_save_during_a_check_leaves_the_quiet_first_check_to_the_next(fake, monkeypatch):
    """A check that runs while the drawer is saved pushes as it started and does not count the searches the Save
    added as checked: their quiet first check is the next one (it once flooded the pushes)."""
    from app.services.search_agents import run_agent
    from tests.fake_platform import listing

    a = create_agent("https://fake.example/s/rad")
    fake.pages = [[listing("1")]]
    await run_agent(a.id)
    _save_during_the_check(monkeypatch, fake, a.id, add="gravel")
    fake.pages = [[listing("1"), listing("4", "Rad neu")]]
    assert [h.ad_id for h in (await run_agent(a.id)).new] == ["4"]
    assert [q.get("quiet") for q in _agent(a.id).queries] == [None, True]     # gravel did not run yet
    fake.pages = [[], [listing("2", "Gravelbike")]]
    assert (await run_agent(a.id)).new == [] and _hit_states(a.id)["2"] == "seen"
    fake.pages = [[], [listing("3", "Gravel neu")]]
    assert [h.ad_id for h in (await run_agent(a.id)).new] == ["3"]

    # a plain agent whose only term is swapped meanwhile, during its first check or a later one: the next is quiet
    for first in (True, False):
        b = create_agent("https://fake.example/s/rad")
        if not first:
            fake.pages = [[listing("b1")]]
            await run_agent(b.id)
        _save_during_the_check(monkeypatch, fake, b.id, drop_terms=[("rad", None, None)], add="gravel")
        fake.pages = [[listing("b1")]]
        await run_agent(b.id)
        assert _agent(b.id).spec["keyword"] == "gravel" and not _agent(b.id).baseline_done
        fake.pages = [[listing("b2", "Gravelbike")]]
        res = await run_agent(b.id)
        assert res.baseline and res.new == [] and _agent(b.id).baseline_done


async def test_a_save_during_a_check_that_leaves_one_quiet_search_it_runs(fake, monkeypatch):
    """A Save during a check drops every search but a quiet one that check runs: a plain agent then, its quiet flag
    the agent's. That check was its first, so the next one pushes (it once muted the next one too)."""
    from app.services.search_agents import run_agent
    from tests.fake_platform import listing

    a = create_agent("https://fake.example/s/rad")
    fake.pages = [[listing("1")]]
    await run_agent(a.id)
    a = _agent(a.id)
    edit_searches(a, add="gravel")
    with session() as db:
        db.add(a)
        db.commit()
    _save_during_the_check(monkeypatch, fake, a.id, drop_terms=[("rad", None, None)])
    fake.pages = [[listing("1")], [listing("2", "Gravelbike")]]                # rad, then gravel (quiet)
    assert (await run_agent(a.id)).new == [] and _hit_states(a.id)["2"] == "seen"
    b = _agent(a.id)
    assert b.queries is None and b.spec["keyword"] == "gravel" and b.baseline_done
    fake.pages = [[listing("3", "Gravel neu")]]
    res = await run_agent(a.id)
    assert not res.baseline and [h.ad_id for h in res.new] == ["3"]


def test_a_save_keeps_the_searches_on_a_site_without_a_chip(client, fake):
    """A watched site that is gone (taken out of PLATFORMS_ENABLED) gets no chip in the drawer, so nothing can untick
    it: Save keeps its searches, and an agent only there can still be renamed."""
    from app.services.search_agents import agent_search_items, create_agent_from_spec

    old = FakePlatform()
    old.id, old.name = "old", "Old"
    platforms.set_platform("old", old)
    a = _settled(URL)
    edit_searches(a, sites=["willhaben", "old"])
    with session() as db:
        db.add(a)
        db.commit()
    b = create_agent_from_spec(old.keyword_search("rad"))
    platforms.set_platform("old", None)
    assert agent_search_items(a)["js"]["sites"] == ["willhaben", "old"] and agent_search_items(a)["per_check"] == 2
    page = client.get("/agents").text
    assert 'name="sites" value="fake"' in page and 'name="sites" value="old"' not in page
    r = _edit(client, _agent(a.id), sites_shown="1", sites=["willhaben"], name="Rad")    # a rename
    assert r.headers["location"] == "/agents" and _agent(a.id).name == "Rad"
    assert [(q["platform"], q["keyword"]) for q in _agent(a.id).queries] == [("willhaben", "fahrrad"), ("old", "fahrrad")]
    r = _edit(client, _agent(b.id), sites_shown="1", name="Rad alt")           # no chip ticked
    assert r.headers["location"] == "/agents" and _agent(b.id).name == "Rad alt" and _agent(b.id).spec["keyword"] == "rad"


def test_the_last_search_on_a_site_without_a_chip_can_go(client, fake):
    """Dropping the last search on a gone site (no chip) drops the site with it: every such Save was once refused
    ("Unknown platform"), the name and interval sent with it too. Unticking every chip still needs a site: one without
    a chip cannot be the only one left."""
    from app.services.search_agents import agent_search_items

    old = FakePlatform()
    old.id, old.name = "old", "Old"
    platforms.set_platform("old", old)
    on_both, with_filter, unticked, plain = (_settled(URL) for _ in range(4))
    for a in (on_both, with_filter, unticked, plain):
        edit_searches(a, sites=["willhaben", "old"])
    set_searches(with_filter, [agent_searches(with_filter)[0], {"platform": "old", "keyword": "",
                                                                "source_url": "https://fake.example/s/?cat=1"}])
    with session() as db:
        for a in (on_both, with_filter, unticked, plain):
            db.add(a)
        db.commit()
    platforms.set_platform("old", None)

    r = _edit(client, _agent(on_both.id), sites_shown="1", sites=["willhaben"], drop_term='["fahrrad", null, null]',
              add_keyword="gravel", name="Gravel", interval_min="30")
    a = _agent(on_both.id)
    assert r.headers["location"] == "/agents" and a.name == "Gravel" and a.interval_s == 1800
    assert a.queries is None and (a.platform, a.spec["keyword"]) == ("willhaben", "gravel")
    r = _edit(client, _agent(with_filter.id), sites_shown="1", sites=["willhaben"], drop_query="1")   # the filter link
    assert r.headers["location"] == "/agents" and _agent(with_filter.id).queries is None
    assert agent_platforms(_agent(with_filter.id)) == ["willhaben"]
    a = _agent(plain.id)                                                       # one chip: the drawer sends no sites
    assert edit_searches(a, drop_terms=[("fahrrad", None, None)], add="gravel") is True
    assert [(q["platform"], q["keyword"]) for q in agent_searches(a)] == [("willhaben", "gravel")]

    assert agent_search_items(_agent(unticked.id))["js"]["fixed"] == ["old"]
    assert 'x-show="noSite()"' in client.get("/agents").text                  # the hint counts the chips only
    r = _edit(client, _agent(unticked.id), sites_shown="1", name="Nur alt")    # no chip ticked
    assert r.headers["location"] == "/agents?error=Choose%20at%20least%20one%20site."
    assert agent_platforms(_agent(unticked.id)) == ["willhaben", "old"] and _agent(unticked.id).name != "Nur alt"


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
