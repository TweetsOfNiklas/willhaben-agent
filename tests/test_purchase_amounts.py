"""The amount guard of our own purchase messages: every way of writing euros counts, and an item price is held to what
the maximum leaves after shipping and the buyer protection fee."""
from app.db import Purchase
from app.services import purchase


def test_every_way_of_writing_euros_is_read():
    assert purchase.amounts("Würden Sie 120 Euro nehmen? Oder 110,- bzw. 105.- und EUR 100, € 95,50, 90 €") == \
        [120, 110, 105, 100, 95.5, 90]
    assert purchase.amounts("Abholung 1150 Wien, um 18 Uhr, Größe 42") == []
    assert purchase.amounts("Seite 3-4") == []


def test_an_item_price_is_held_to_the_item_ceiling_a_total_to_the_maximum():
    row = Purchase(ad_id="1", mode="paylivery", status="negotiating", max_price=100.0, shipping_cost=6.0)
    item_max = purchase.item_ceiling(row)
    assert item_max < 94
    tp = lambda text: purchase.text_problems(text, max_price=100.0, address_ok=False, item_max=item_max)  # noqa: E731
    assert tp("Würden Sie 98 Euro nehmen?")                        # item price over what the maximum leaves
    assert tp("Ich biete 98,-")
    assert not tp("Ich biete 85 €")
    assert not tp("Also 98 € inkl. Versand und Käuferschutz?")     # a total: up to the maximum
    assert not tp("Insgesamt 99 €, passt das?")
    assert tp("Insgesamt 101 €, passt das?")
