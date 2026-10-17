"""Shipping to the owner: what each site states in its result list or on the ad page, and how it is shown."""
import pytest

from app.platforms import ebay, facebook, kleinanzeigen, preisjaeger, shpock
from app.platforms import shipping_info as si
from tests.conftest import FIXTURES, load


@pytest.mark.parametrize("text, expected", [
    ("+EUR 5,00 Versand", (5.0, "ship")), ("+EUR 59,99 Versand", (59.99, "ship")),
    ("Kostenloser Versand", (0.0, "ship")), ("Gratisversand", (0.0, "ship")), ("Nur Abholung", (None, "pickup")),
    ("Versand möglich", (None, "ship")), ("+EUR 1.234,50 Versand", (1234.5, "ship")), ("", (None, None)),
    ("oder Preisvorschlag", (None, None)),
])
def test_from_text(text, expected):
    assert si.from_text(text) == expected


def test_labels_and_prompt():
    assert si.label(5.0, "ship") == "+ € 5,00 shipping" and si.label(0.0, "ship") == "free shipping"
    assert si.label(None, "pickup") == "pickup only" and si.label(None, "ship") == "shipping possible"
    assert si.label(None, None) == "" and si.label_of({}) == "" and si.label_of({"shipping": "pickup"}) == "pickup only" and si.total(20.0, 4.5) == 24.5 and si.total(20.0, None) is None
    assert si.prompt_line(4.5, "ship").startswith("Versand: 4.5 €") and si.prompt_line(None, None) == "Versand: nicht angegeben"


def test_apply_keeps_what_the_list_does_not_state():
    class O:
        shipping_price = None
        shipping = None

    hit, page, plain = O(), O(), O()
    page.shipping_price = 4.9
    si.apply_shipping(hit, page)
    assert (hit.shipping_price, hit.shipping) == (4.9, "ship")
    si.apply_shipping(hit, plain)                          # a list without shipping: kept
    assert (hit.shipping_price, hit.shipping) == (4.9, "ship")
    plain.shipping = "pickup"
    si.apply_shipping(hit, plain)
    assert (hit.shipping_price, hit.shipping) == (None, "pickup")


def test_the_sites_state_it():
    ads = ebay.parse_search((FIXTURES / "ebay_search.html").read_text(encoding="utf-8")).ads
    assert {(a.shipping_price, a.shipping) for a in ads} >= {(5.0, "ship"), (0.0, "ship")}
    assert kleinanzeigen.parse_ad(load("kleinanzeigen_ad_handy.json")).shipping == "pickup"
    assert {a.shipping for a in shpock.parse_search(load("shpock_search.json")).ads} <= {"ship", "pickup", None}
    fb = facebook.parse_search((FIXTURES / "facebook_search.html").read_text(encoding="utf-8")).ads
    assert "pickup" in {a.shipping for a in fb}
    pj = preisjaeger.parse_search((FIXTURES / "preisjaeger_search.html").read_text(encoding="utf-8")).ads
    assert all(a.shipping in (None, "ship") for a in pj)


def test_ebay_asks_for_the_owners_postcode():
    from app.services import shipping

    assert ebay.ship_to() == {}
    shipping.save_sender({"name": "Max Muster", "street": "Musterweg", "house_no": "1", "zip": "1010", "city": "Wien"})
    assert ebay.ship_to() == {"_stpos": "1010"}


async def test_a_pushed_hit_gets_its_shipping_from_the_ad_page(monkeypatch):
    """The list states no shipping: the hit about to be pushed has its ad page loaded, and the push names it."""
    from app import platforms
    from app.db import Hit, session
    from app.services import search_agents
    from app.services.search_agents import create_agent, run_agent
    from sqlmodel import select
    from tests.fake_platform import FakePlatform, detail_of, listing

    sent = []

    async def notify(kind, title, text="", **kw):
        sent.append((kind, title, kw.get("data") or {}))

    monkeypatch.setattr(search_agents, "notify", notify)
    fake = FakePlatform()
    platforms.set_platform("fake", fake)
    try:
        a = create_agent("https://fake.example/s/rad")
        fake.pages = [[]]
        await run_agent(a.id)
        d = detail_of(listing("1", "Rennrad", 300.0))
        d.shipping_price = 4.9
        fake.details = {"1": d}
        fake.pages = [[listing("1", "Rennrad", 300.0)]]
        await run_agent(a.id)
        assert fake.detail_calls == ["1"]
        kind, title, data = sent[-1]
        assert kind == "hit" and "+ € 4,90 shipping" in title and data["shipping_price"] == 4.9
        with session() as db:
            h = db.exec(select(Hit)).one()
            assert (h.shipping_price, h.shipping, h.site_status) == (4.9, "ship", "active")
        fake.pages = [[listing("1", "Rennrad", 300.0)]]                 # the next list keeps what the page told
        await run_agent(a.id)
        with session() as db:
            assert db.exec(select(Hit)).one().shipping_price == 4.9
    finally:
        platforms.reset()
