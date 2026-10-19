"""Buyer fees of the sites' payment services (app/platforms/fees.py)."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.platforms import fees, shipping_info
from app.platforms import vinted as vi

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize("platform, item, fee", [
    ("willhaben", 10, 0.99), ("willhaben", 100, 4.5), ("willhaben", 5000, 49.99),
    # Vinted: 5 % + 0.70, as its own list states it (2.00 -> 0.80, 8.99 -> 1.15, 20.00 -> 1.70), no cap
    ("vinted", 2.0, 0.8), ("vinted", 8.99, 1.15), ("vinted", 20.0, 1.7), ("vinted", 3000, 150.7),
    ("kleinanzeigen", 50, 2.75), ("kleinanzeigen", 200, 9.5),
    ("ebay", 100, 0.0), ("shpock", 100, 0.0), (None, 100, 4.5),
])
def test_fee_of_each_site(platform, item, fee):
    assert fees.fee(platform, item) == fee


def test_a_stated_fee_beats_the_estimate():
    assert fees.fee("vinted", 20.0, known=1.85) == 1.85


def test_category_rule_leaf_first(monkeypatch):
    phones = fees.FeeRule("willhaben Käuferschutzgebühr", 6.0, 0.0, 0.99, 49.99, category="handys")
    monkeypatch.setitem(fees.CATEGORY_RULES, "willhaben", {"handys": phones})
    assert fees.rule("willhaben", ["Marktplatz", "Smartphones / Telefonie", "Handys"]) is phones
    assert fees.rule("willhaben", ["Marktplatz", "Audio / HiFi"]) is fees.RULES["willhaben"]
    assert fees.fee("willhaben", 100, ["Handys"]) == 6.0
    assert fees.rule("vinted", ["Handys"]) is fees.RULES["vinted"]      # another site's category rule never applies


@pytest.mark.parametrize("platform", ["willhaben", "vinted", "kleinanzeigen"])
def test_item_for_cents_is_the_inverse(platform):
    r = fees.RULES[platform]
    for avail in (0, 50, 99, 100, 171, 2300, 10450, 31940, 116100, 700000):
        c = r.item_for_cents(avail)
        assert c + r.cents(c) <= avail or c == 0
        assert (c + 1) + r.cents(c + 1) > avail


def test_from_dict_round_trip_and_fallback():
    r = fees.RULES["vinted"]
    assert fees.from_dict(r.as_dict()) == r
    assert fees.from_dict(None, "kleinanzeigen") is fees.RULES["kleinanzeigen"]
    assert fees.from_dict({"pct": "x"}, "willhaben") is fees.RULES["willhaben"]


def test_prompt_text():
    assert fees.text_de(fees.RULES["willhaben"]) == ("willhaben Käuferschutzgebühr 4,5 % des Artikelpreises, mind. 0,99 €, "
                                                     "max. 49,99 €, zahlt der Käufer")
    assert fees.text_de(fees.RULES["vinted"]) == "Vinted Käuferschutzgebühr 5 % des Artikelpreises + 0,70 €, zahlt der Käufer"


def test_vinted_list_states_the_fee_and_the_label_names_it():
    first = vi.parse_catalog((FIXTURES / "vinted_catalog.rsc").read_text(encoding="utf-8")).ads[0]
    assert first.price == 2.0 and first.buyer_fee == 0.8 and first.as_dict()["buyer_fee"] == 0.8
    hit = SimpleNamespace(shipping_price=None, shipping=None, buyer_fee=None)
    shipping_info.apply_shipping(hit, first)
    assert hit.buyer_fee == 0.8
    assert shipping_info.label(4.9, "ship") == "+ € 4,90 shipping"
    assert shipping_info.label_of({"shipping_price": 4.9, "shipping": "ship", "buyer_fee": 0.8}) == \
        "+ € 4,90 shipping · + € 0,80 buyer protection"
    assert shipping_info.label_of({"buyer_fee": 1.15}) == "+ € 1,15 buyer protection"
    assert shipping_info.label_of({"shipping_price": 4.9, "shipping": "ship"}) == "+ € 4,90 shipping"
