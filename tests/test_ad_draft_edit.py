"""The ad draft page: fields saved when left, a revision that changes product or price, the description in willhaben's
editor format, the PayLivery offers of the handover form."""
from app.db import AdDraft, session
from app.services import ad_creator
from app.willhaben import insert
from tests.conftest import wait_jobs
from tests.test_dashboard import client  # noqa: F401  (pytest fixture)
from tests.test_geizhals import gh  # noqa: F401  (pytest fixture)


def _new(client, notes="Bosch Akkuschrauber"):  # noqa: F811
    r = client.post("/new-ad", data={"notes": notes}, follow_redirects=False)
    return int(r.headers["location"].rsplit("/", 1)[1])


def _get(draft_id):
    with session() as db:
        return db.get(AdDraft, draft_id)


def test_a_field_is_saved_when_left(client):  # noqa: F811
    draft_id = _new(client)
    assert 'id="autosave-state"' in client.get(f"/new-ad/{draft_id}").text
    assert client.post(f"/new-ad/{draft_id}/fields", data={"price": "1.200,50"}).json() == {"ok": True, "error": None}
    d = _get(draft_id)
    assert d.price_suggestion == 1200.5 and d.title == "KTM Kinderrad 20 Zoll blau"     # only the field sent
    assert 'value="1200.5"' in client.get(f"/new-ad/{draft_id}").text                     # a reload keeps it
    r = client.post(f"/new-ad/{draft_id}/fields", data={"price": "zwölf"}).json()
    assert r["ok"] is False and "zwölf" in r["error"] and _get(draft_id).price_suggestion == 1200.5
    assert _get(draft_id).error is None                                                   # no banner for it
    client.post(f"/new-ad/{draft_id}/fields", data={"description": "Zeile 1\r\n\r\nZeile 2", "quantity": "3"})
    d = _get(draft_id)
    assert d.description == "Zeile 1\n\nZeile 2" and d.quantity == 3


def test_while_claude_revises_only_price_and_pieces_are_saved(client):  # noqa: F811
    draft_id = _new(client)
    ad_creator.update_draft(draft_id, refining=True)
    r = client.post(f"/new-ad/{draft_id}/fields", data={"price": "80", "title": "Neu"}).json()
    d = _get(draft_id)
    assert d.price_suggestion == 80 and d.title == "KTM Kinderrad 20 Zoll blau" and r["ok"] is False
    ad_creator.update_draft(draft_id, refining=False, status="published")
    assert client.post(f"/new-ad/{draft_id}/fields", data={"price": "90"}).json()["ok"] is False
    assert _get(draft_id).price_suggestion == 80


def test_a_revision_names_another_product_and_a_price(client, gh, monkeypatch):  # noqa: F811
    fake = client.fake
    orig = fake.complete

    async def complete(**kw):
        r = await orig(**kw)
        if "title" in r.data:
            r.data = {**r.data, "product_query": "Bosch GSR 18V-55", "price_estimate": 999}
            if "Änderungswünsche: Es ist das Set" in kw["prompt"]:
                r.data = {**r.data, "product_query": "Bosch GSR 18V-55 Set", "price_estimate": 150}
        return r

    monkeypatch.setattr(fake, "complete", complete)
    draft_id = _new(client)
    client.post(f"/new-ad/{draft_id}/fields", data={"price": "120"})
    searches = len(gh)

    client.post(f"/new-ad/{draft_id}/refine", data={"instruction": "kürzer"})   # Claude's own estimate: price stays
    wait_jobs()
    prompt = fake.calls[-1]["prompt"]
    assert "- Produkt (für Geizhals): Bosch GSR 18V-55" in prompt and "- Preis: 120 €" in prompt
    d = _get(draft_id)
    assert d.price_suggestion == 120 and d.product_query == "Bosch GSR 18V-55" and len(gh) == searches

    client.post(f"/new-ad/{draft_id}/refine", data={"instruction": "Es ist das Set, Preis 150 €"})
    wait_jobs()
    d = _get(draft_id)
    assert d.price_suggestion == 150 and d.product_query == "Bosch GSR 18V-55 Set"
    assert len(gh) > searches and "fs=Bosch+GSR+18V-55+Set" in gh[searches]           # Geizhals looked up again
    assert d.geizhals["query"] == "Bosch GSR 18V-55 Set"

    client.post(f"/new-ad/{draft_id}/undo")
    d = _get(draft_id)
    assert d.price_suggestion == 120 and d.product_query == "Bosch GSR 18V-55"


def test_named_price():
    assert ad_creator.named_price("Preis 1.200 €", 1200) == 1200
    assert ad_creator.named_price("um 12,50 verkaufen", "12.5") == 12.5
    assert ad_creator.named_price("kürzer", 120) is None
    assert ad_creator.named_price("Akku von 2024", 2024) == 2024 and ad_creator.named_price("x", None) is None


def test_description_in_willhabens_editor_format():
    text = "Erster Absatz.\n\nZweiter Absatz,\nzweite Zeile.\n\n\n• Netzteil\n• Tasche\nEnde"
    html = insert.description_html(text)
    assert html == ("<p>Erster Absatz.</p><p><br></p><p>Zweiter Absatz,</p><p>zweite Zeile.</p><p><br></p>"
                    "<ul><li>Netzteil</li><li>Tasche</li></ul><p>Ende</p>")
    assert insert.description_text(html) == "Erster Absatz.\n\nZweiter Absatz,\nzweite Zeile.\n\n• Netzteil\n• Tasche\nEnde"
    assert insert.description_text("<p>A</p><p>B</p>") == "A\nB"           # older ads: as willhaben shows them
    assert insert.description_text("<p>A</p><p><br></p><p>B &amp; C</p>") == "A\n\nB & C"


OPTIONS = {"deliveryOptions": [
    {"uuid": "u1", "price": 5.7, "originalPrice": 5.7, "description": "max. L 100cm × B 60cm × H 60cm, bis 3kg, quaderförmiges Paket",
     "parcelInsurance": "Haftung bis € 510", "parcelSize": {"size": "S"}, "carrier": {"description": "Post"}},
    {"uuid": "u2", "price": 3.49, "originalPrice": 5.6, "description": "Gurtmaß max. 3m, bis 3kg, quaderförmiges Paket",
     "parcelInsurance": "Haftung bis € 520", "parcelSize": {"size": "S"}, "carrier": {"description": "dpd"}},
    {"uuid": "u3", "price": 6.9, "originalPrice": 6.9, "description": "Längste + kürzeste Seite = max. 120cm, bis 31,5kg",
     "parcelSize": {"size": "L"}, "carrier": {"description": "Hermes"}},
]}


def test_offers_of_willhabens_list_and_the_preselection():
    offers = insert.offers_of(OPTIONS)
    assert [(o["size"], o["carrier"], o["price"]) for o in offers] == [("S", "dpd", 3.49), ("S", "Post", 5.7), ("L", "Hermes", 6.9)]
    assert offers[0]["original_price"] == 5.6 and "original_price" not in offers[1]
    assert offers[0]["max_kg"] == 3.0 and offers[2]["max_kg"] == 31.5 and offers[1]["limits"].endswith("bis 3kg")
    assert insert.preselect_offer(offers, None)["carrier"] == "dpd"
    assert insert.preselect_offer(offers, 8)["carrier"] == "Hermes"                     # too heavy for S
    assert insert.preselect_offer(offers, None, "S", "post")["carrier"] == "Post"       # chosen before
    assert insert.preselect_offer(offers, None, "L", None)["carrier"] == "Hermes"        # size with "cheapest"
    assert insert.preselect_offer(insert.OFFERS, 2.0)["carrier"] == "dpd"


def test_the_live_list_replaces_the_stored_one(client):  # noqa: F811
    assert ad_creator.paylivery_offers() == insert.OFFERS
    ad_creator.remember_offers({"trace": {"delivery": {"offers": insert.offers_of(OPTIONS)}}})
    assert [o["carrier"] for o in ad_creator.paylivery_offers()] == ["dpd", "Post", "Hermes"]
    ad_creator.remember_offers({"trace": {}})                                            # pickup: kept
    assert len(ad_creator.paylivery_offers()) == 3


def test_parse_shipping_takes_one_offer():
    form = {"handover": "shipping", "paylivery": "1", "parcel_option": "m|Hermes"}
    assert ad_creator.parse_shipping(form) == {"handover": "shipping", "paylivery": True, "size": "M", "carrier": "Hermes"}
    bogus = {"handover": "both", "parcel_option": "XXL|UPS"}
    assert ad_creator.parse_shipping(bogus) == {"handover": "both", "paylivery": False, "size": None, "carrier": None}
