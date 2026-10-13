"""Links and labels name the right site: agent thumbnails, the hits badges of multi-site agents, the viewer opened from a
push or the timeline, the agent builder with a pasted link or a price range, the screening prompt of a multi-site
batch and the planned searches of a product search."""
import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app import platforms
from app.db import Hit, ProductSearch, SearchAgent, session, set_setting
from app.services import screening, timeline
from tests.fake_platform import FakePlatform


@pytest.fixture
def world():
    """A willhaben agent that also watches a fake site, with one hit on each site; the fake site has no ad details."""
    class NoDetails(FakePlatform):
        capabilities = frozenset({"search_url", "keyword_search", "images"})

    platforms.set_platform("fake", NoDetails())
    with session() as db:
        a = SearchAgent(name="Räder", source_url="https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz?keyword=rad",
                        webapi_url="https://www.willhaben.at/webapi/x?keyword=rad",
                        queries=[{"keyword": "rad", "platform": "willhaben", "source_url": "https://www.willhaben.at/iad/x",
                                  "webapi_url": "https://www.willhaben.at/webapi/x"},
                                 {"keyword": "rad", "platform": "fake", "source_url": "https://fake.example/s/rad",
                                  "webapi_url": "https://fake.example/s/rad"}])
        db.add(a)
        db.commit()
        db.refresh(a)
        db.add(Hit(agent_id=a.id, ad_id="123456789", heading="Rad AT", url="https://www.willhaben.at/iad/d/rad-123456789/"))
        db.add(Hit(agent_id=a.id, platform="fake", ad_id="777", heading="Rad DE", url="https://fake.example/ad/777",
                   price=60.0, body="Fast neu", location="10115 Berlin", state="new"))
        db.commit()
    yield a
    platforms.reset()


@pytest.fixture
def client(world):
    from app.main import app
    with TestClient(app) as c:
        yield c


def test_cards_badges_and_the_viewer_without_a_hit_id(client, world):
    assert 'data-ad="fake:777"' in client.get("/agents").text                      # thumbnails carry the ref
    hits = client.get("/hits?state=all").text
    assert hits.count("top-2 right-2") == 2 and ">Fake</span>" in hits             # badges: the agent has two sites
    page = client.get("/hits/ad/fake/777").text                                    # a push link: no ?hit=
    assert "Rad DE" in page and "no longer online" not in page
    with session() as db:
        assert db.exec(select(Hit).where(Hit.ad_id == "777")).one().state == "seen"   # opened = seen


def test_timeline_links_a_hit_by_its_ref(world):
    with session() as db:
        h = db.exec(select(Hit).where(Hit.platform == "fake")).one()
        wh = db.exec(select(Hit).where(Hit.platform == "willhaben")).one()
    assert timeline.resolve_ids({"hit_id": str(h.id)})["ad_id"] == "fake:777"
    assert timeline.resolve_ids({"hit_id": str(wh.id)})["ad_id"] == "123456789"


def test_screening_names_every_site_of_the_batch():
    from tests.fake_platform import FakePlatform as Fake

    platforms.set_platform("fake", Fake())
    try:
        assert "willhaben.at (österreichisches" in screening._system(["willhaben"])
        both = screening._system(["willhaben", "fake"])
        assert "willhaben.at, fake.example (europäisches" in both
        assert "willhaben.at (österreichisches" in screening._system(["gone"]) and screening._site_name("fake") == "Fake"
    finally:
        platforms.reset()


def test_builder_pasted_link_replaces_the_default_tick(client, monkeypatch):
    set_setting("platform_ebay_enabled", "1")
    from app.platforms import ebay
    from app.platforms.ratelimit import RateLimiter
    from tests.test_platforms_contract import EBAY_ROUTES, FileTransport

    c = ebay.EbayClient(transport=FileTransport(EBAY_ROUTES))
    c.limiter = RateLimiter(0, name="eBay")
    ebay.set_client(c)
    try:
        page = client.get("/agents/new", params=[("url", "https://www.ebay.at/sch/i.html?_nkw=iphone+13"),
                                                  ("sites", "willhaben")]).text
    finally:
        ebay.set_client(None)
    assert 'value="ebay" class="checkbox checkbox-sm" checked' in page
    assert 'value="willhaben" class="checkbox checkbox-sm" checked' not in page


def test_builder_keeps_the_typed_price_for_willhaben(client, monkeypatch):
    seen = []

    async def filters(url):
        seen.append(url)
        return None

    monkeypatch.setattr(platforms.get("willhaben"), "filters", filters)
    client.get("/agents/new", params=[("sites", "willhaben"), ("sites", "fake"), ("keyword", "rtx 3080"),
                                      ("price_from", "300"), ("price_to", "500")])
    assert seen and "PRICE_FROM=300" in seen[0] and "PRICE_TO=500" in seen[0] and "keyword=rtx" in seen[0]


def test_planned_searches_name_each_chosen_site(client):
    set_setting("platform_ebay_enabled", "1")
    plan = {"summary": "Rad", "queries": [{"keyword": "rennrad", "why": "x"}, {"keyword": "gravel", "why": "y"}]}
    with session() as db:
        row = ProductSearch(description="Rad", status="asking", plan=plan, settings={"platforms": ["fake", "ebay"]})
        db.add(row)
        db.commit()
        db.refresh(row)
    frag = client.get(f"/deals/search/{row.id}").text
    table = frag[frag.index("<tbody"):frag.index("</tbody>")]
    # each term once, below it a row with a link per site the run will use; nothing found yet: no result column
    assert table.count("<tr") == 4 and table.count(">Fake<svg") == 2 and table.count(">eBay<svg") == 2
    assert table.count(">gravel<") == 1 and ">Found<" not in frag and "each on 2 sites" in frag
    assert "willhaben" not in table and "https://www.ebay.at/sch/i.html?_nkw=gravel" in table
    set_setting("platform_ebay_enabled", "0")              # switched off before the run: the run skips eBay, so do the rows
    frag = client.get(f"/deals/search/{row.id}").text
    table = frag[frag.index("<tbody"):frag.index("</tbody>")]
    # one site left: no site links, the term itself links to its search there
    assert table.count("<tr") == 2 and "rounded-full" not in table and "ebay" not in table.lower()
    assert 'href="https://fake.example/s/gravel"' in table and table.count('title="Search on Fake"') == 2
    assert "each on" not in frag
