"""One search agent on several sites: one search per site, hits that keep their site, a site that is gone, and the
forms that create such an agent (a search term on several sites, a link plus its term on the others)."""
import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app import platforms
from app.db import Hit, SearchAgent, session
from app.platforms import SearchSpec
from app.services.search_agents import agent_platforms, create_agent_on_sites, run_agent
from tests.fake_platform import FakePlatform, listing


class OtherFake(FakePlatform):
    id = "other"
    name = "Other"
    host = "other.example"
    hosts = ("other.example",)
    image_hosts = ()

    def parse_search_url(self, url: str) -> SearchSpec:
        spec = super().parse_search_url(url.replace("other.example", "fake.example"))
        return SearchSpec(platform=self.id, url=spec.url.replace("fake.example", "other.example"), keyword=spec.keyword)

    def matches(self, url: str) -> bool:
        return "other.example" in url

    def keyword_search(self, keyword, price_from=None, price_to=None, *, category=None, sort="newest") -> SearchSpec:
        return SearchSpec(platform=self.id, url=f"https://other.example/s/{keyword}", keyword=keyword,
                          price_from=price_from, price_to=price_to, sort=sort)


@pytest.fixture
def sites():
    a, b = FakePlatform(), OtherFake()
    platforms.set_platform("fake", a)
    platforms.set_platform("other", b)
    yield a, b
    platforms.reset()


@pytest.fixture
def client(sites):
    from app.main import app
    with TestClient(app) as c:
        yield c


def _hits(agent_id: int) -> dict[tuple[str, str], Hit]:
    with session() as db:
        return {(h.platform, h.ad_id): h for h in db.exec(select(Hit).where(Hit.agent_id == agent_id))}


async def test_one_agent_watches_two_sites(sites):
    a, b = sites
    agent = create_agent_on_sites([a.keyword_search("rad", None, 300), b.keyword_search("rad", None, 300)])
    assert agent.platform == "fake" and agent_platforms(agent) == ["fake", "other"] and len(agent.queries) == 2
    assert [q["platform"] for q in agent.queries] == ["fake", "other"] and agent.queries[1]["price_to"] == 300
    a.pages = [[listing("1", "Rad A", 100.0)]]
    b.pages = [[listing("1", "Rad B", 120.0), listing("2", "Rad C", 90.0)]]   # "1" exists on both sites
    res = await run_agent(agent.id)
    assert res.baseline and res.error is None
    assert set(_hits(agent.id)) == {("fake", "1"), ("other", "2")}             # the same id on the other site: skipped
    assert [s.keyword for s in a.searched + b.searched] == ["rad", "rad"]
    b.pages = [[listing("2", "Rad C", 70.0), listing("3", "Rad D", 50.0)]]
    res = await run_agent(agent.id)
    assert [(h.platform, h.ad_id) for h in res.new] == [("other", "3")]
    assert [(h.platform, h.ad_id) for h in res.price_drops] == [("other", "2")]
    assert platforms.multi() is True


async def test_a_site_that_is_gone_is_skipped_not_fatal(sites):
    a, b = sites
    agent = create_agent_on_sites([a.keyword_search("rad"), b.keyword_search("rad")])
    a.pages = [[listing("1", "Rad A", 100.0)]]
    platforms.set_platform("other", None)
    res = await run_agent(agent.id)
    with session() as db:
        row = db.get(SearchAgent, agent.id)
    assert res.error is None and set(_hits(agent.id)) == {("fake", "1")}
    assert "Unknown platform: other" in (row.last_error or "")
    platforms.set_platform("fake", None)
    assert "Unknown platform" in (await run_agent(agent.id)).error             # nothing left to search



async def test_a_search_the_vpn_holds_is_no_error(sites):
    """A shuffle under way or a site paused on the current VPN server holds its searches: the other site's run as
    usual, the agent shows no error, and a run the VPN held completely leaves the last result as it was."""
    from app.platforms.vpn import VpnWait

    a, b = sites
    agent = create_agent_on_sites([a.keyword_search("rad"), b.keyword_search("rad")])
    a.pages = [[listing("1", "Rad A", 100.0)]]
    b.pages = [VpnWait("The VPN is changing its server; Other waits for it")]
    res = await run_agent(agent.id)
    with session() as db:
        row = db.get(SearchAgent, agent.id)
    assert res.error is None and row.last_error is None and set(_hits(agent.id)) == {("fake", "1")}
    a.pages = [VpnWait("Fake refused the VPN server at-vie-wg-001 (blocked by the site)")]
    res = await run_agent(agent.id)
    with session() as db:
        again = db.get(SearchAgent, agent.id)
    assert res.error is None and again.last_error is None and again.last_run_at == row.last_run_at

def test_create_form_with_several_sites(client):
    r = client.post("/agents", data={"keyword": "rennrad", "price_to": "500", "sites": ["fake", "other"],
                                     "interval_min": "10"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/agents"
    r = client.post("/agents", data={"url": "https://fake.example/s/kinderrad", "sites": ["fake", "other"]},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/agents"
    with session() as db:
        first, second = db.exec(select(SearchAgent).order_by(SearchAgent.id)).all()
    assert [(q["platform"], q["keyword"], q["price_to"]) for q in first.queries] == [("fake", "rennrad", 500), ("other", "rennrad", 500)]
    assert first.interval_s == 600 and first.name == "rennrad"
    assert [(q["platform"], q["keyword"]) for q in second.queries] == [("fake", "kinderrad"), ("other", "kinderrad")]
    r = client.post("/agents", data={"url": "https://fake.example/s/", "sites": ["other"]}, follow_redirects=False)
    assert "Give%20a%20search%20term%20for%20Other" in r.headers["location"]
    api = client.post("/api/agents", json={"keyword": "roller", "sites": ["other", "fake"]}).json()
    assert [q["platform"] for q in api["queries"]] == ["other", "fake"] and api["platform"] == "other"
    one = client.post("/api/agents", json={"keyword": "roller", "platform": "fake"}).json()
    assert one["queries"] is None and one["platform"] == "fake"                # a single site stays a plain agent
    assert "Fake · Other" in client.get("/agents").text


def test_new_agent_page_previews_every_chosen_site(sites, client):
    a, b = sites
    a.pages = [[listing("1", "Rad A", 100.0)]]
    b.pages = [[listing("7", "Rad B", 120.0)]]
    page = client.get("/agents/new?sites=fake&sites=other&keyword=rad").text
    assert "main search" in page and "Rad A" in page and "Rad B" in page and ">Other<" in page
    form = page[page.index('action="/agents"'):]
    assert 'name="sites" value="fake"' in form and 'name="sites" value="other"' in form and 'name="keyword" value="rad"' in form
    alone = client.get("/agents/new?sites=other&keyword=rad").text
    assert "main search" not in alone and 'name="sites" value="other" class="checkbox checkbox-sm" checked' in alone
