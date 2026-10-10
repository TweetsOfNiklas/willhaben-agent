"""Setting: always attach the Geizhals spec sheet to new listings when the product was found."""
from app.db import AdDraft, get_setting, session
from app.services import ad_creator, geizhals
from tests.test_dashboard import client  # noqa: F401  (pytest fixture)

MATCH = {"name": "Muster Lautsprecher X1", "url": "https://geizhals.at/x1-a1.html", "price_from": None,
         "specs": [["Farbe", "weiß"], ["Akku", "10h"]]}


def _draft(desc="Gut erhalten."):
    with session() as db:
        d = AdDraft(input_text="", title="Lautsprecher", description=desc, image_paths=[],
                    geizhals={"query": "x1", "match": MATCH, "candidates": []})
        db.add(d)
        db.commit()
        db.refresh(d)
    return d


def test_attach_only_when_enabled_and_once(client):  # noqa: F811
    d = _draft()
    assert ad_creator.attach_specs_if_wanted(d.id).description == "Gut erhalten."          # setting off
    client.post("/settings/listings", data={"geizhals_auto_specs": "on"})
    assert get_setting("geizhals_auto_specs") == "1" and "checked" in client.get("/settings/rules").text.split(
        'name="geizhals_auto_specs"')[1][:80]
    block = geizhals.spec_block(MATCH)
    assert ad_creator.attach_specs_if_wanted(d.id).description == f"Gut erhalten.\n\n{block}"
    assert ad_creator.attach_specs_if_wanted(d.id).description.count("Technische Daten") == 1   # never twice
    client.post("/settings/listings", data={})
    assert get_setting("geizhals_auto_specs") == "0"


async def test_choosing_another_product_replaces_the_sheet(monkeypatch):
    other = geizhals.Product(name="Muster Lautsprecher X2", url="https://geizhals.at/x2-a2.html", price_from=None,
                             specs=[("Farbe", "schwarz")]) if hasattr(geizhals, "Product") else None
    if other is None:
        import pytest
        pytest.skip("geizhals.Product not available")

    async def fake_product(url):
        return other

    monkeypatch.setattr(geizhals, "product", fake_product)
    d = _draft(f"Gut erhalten.\n\n{geizhals.spec_block(MATCH)}")      # user had ticked the old sheet
    d = await ad_creator.choose_geizhals(d.id, other.url)
    assert "X1" not in d.description and "Muster Lautsprecher X2" in d.description and "schwarz" in d.description
    d2 = _draft()                                                      # no sheet before, setting off: none after
    d2 = await ad_creator.choose_geizhals(d2.id, other.url)
    assert d2.description == "Gut erhalten."
