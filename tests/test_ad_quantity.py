"""Pieces and parcel estimate on ad drafts: Claude's answer, the owner's count, the draft forms and the prompts."""
import json

import pytest

from app import db as dbmod
from app.llm.base import LLMError
from app.llm.prompts import AD_SCHEMA, SORT_SCHEMA, ad_prompt, ad_refine_prompt, sort_prompt
from app.services import ad_creator
from tests.test_dashboard import client  # noqa: F401  (pytest fixture)
from tests.conftest import wait_jobs

PARCEL = {"length_cm": 120, "width_cm": 20, "height_cm": 70, "weight_kg": 14.2}


def _strict(schema, path="$"):
    """Every object in a schema for Claude lists all its keys as required and allows no others."""
    if "properties" in schema:
        assert schema["additionalProperties"] is False, path
        assert set(schema["required"]) == set(schema["properties"]), path
        for key, sub in schema["properties"].items():
            _strict(sub, f"{path}.{key}")
    if "items" in schema:
        _strict(schema["items"], f"{path}[]")


def test_claude_schemas_stay_strict_and_carry_the_new_keys():
    for schema in (AD_SCHEMA, SORT_SCHEMA):
        _strict(schema)
        assert "minimum" not in json.dumps(schema)           # numeric limits are not part of the strict subset
    assert {"quantity", "parcel"} <= set(AD_SCHEMA["required"])
    assert set(AD_SCHEMA["properties"]["parcel"]["required"]) == {"length_cm", "width_cm", "height_cm", "weight_kg"}
    assert "quantity" in SORT_SCHEMA["properties"]["groups"]["items"]["required"]


def test_prompts_name_the_owners_count():
    line = "Stückzahl: 4 (Preis pro Stück) – erwähne im Text, dass 4 Stück verfügbar sind"
    assert line in ad_prompt("Stühle", "", 3, quantity=4)
    for q in (None, 0):
        assert "Stückzahl" not in ad_prompt("Stuhl", "", 3, quantity=q)
    one = ad_prompt("Stuhl", "", 3, quantity=1)                         # the owner's explicit "one item"
    assert "Stückzahl: 1 – ein einzelnes Stück, auch wenn auf den Fotos mehrere zu sehen sind" in one
    assert "Preis pro Stück" not in one
    refine = dict(notes="", rules="", n_images=1, current={}, specs=None, product_name=None, instruction="kürzer")
    assert line in ad_refine_prompt(**refine, quantity=4) and "Stückzahl" not in ad_refine_prompt(**refine)
    groups = [{"photos": [1, 2], "product": "Stuhl", "quantity": 4}, {"photos": [3], "product": "Tisch", "quantity": None}]
    prompt = sort_prompt(notes="", n_images=3, current=groups)
    assert "Fotos 1, 2: Stuhl (4 Stück)" in prompt and "Fotos 3: Tisch" in prompt and "None" not in prompt


@pytest.mark.parametrize("raw,want", [("4", 4), (" 12 ", 12), ("1", 1), ("", None), (None, None), ("0", None),
                                      ("-3", None), ("abc", None), ("2.5", None), ("5000", 999), (7, 7), (3.0, 3), (2.5, None)])
def test_parse_quantity(raw, want):
    assert ad_creator.parse_quantity(raw) == want


@pytest.mark.parametrize("owner,claude,want", [(None, 3, 3), (None, 1, None), (None, None, None), (None, 0, None),
                                               (None, "x", None), (2, 3, 2), (1, 3, None), (5, None, 5)])
def test_pick_quantity_owner_wins(owner, claude, want):
    assert ad_creator.pick_quantity(owner, claude) == want


async def test_create_draft_takes_claudes_count_and_parcel(client):  # noqa: F811
    client.fake.ad_extra = {"quantity": 3}
    d = await ad_creator.create_draft("3 Stück KTM Kinderrad", [])
    assert d.quantity == 3
    assert d.parcel == {"length_cm": 120, "width_cm": 70, "height_cm": 20, "weight_kg": 14.2,
                        "contents": "KTM Kinderrad 20 Zoll blau", "source": "claude"}
    assert "Stückzahl" not in client.fake.calls[-1]["prompt"]       # the owner named none

    client.fake.ad_extra = {"quantity": 1}
    assert (await ad_creator.create_draft("ein Rad", [])).quantity is None    # one piece is a single item


async def test_create_draft_owners_count_wins(client):  # noqa: F811
    client.fake.ad_extra = {"quantity": 3}
    d = await ad_creator.create_draft("Kinderräder", [], quantity=2)
    assert d.quantity == 2
    assert ("Stückzahl: 2 (Preis pro Stück) – erwähne im Text, dass 2 Stück verfügbar sind"
            in client.fake.calls[-1]["prompt"])

    d = await ad_creator.create_draft("Kinderräder", [], quantity=1)      # the owner says: just one
    assert d.quantity is None and "Stückzahl: 1 – ein einzelnes Stück" in client.fake.calls[-1]["prompt"]


async def test_create_draft_parcel_that_claude_could_not_estimate(client):  # noqa: F811
    client.fake.ad_extra = {"parcel": {"length_cm": 0, "width_cm": 0, "height_cm": 0, "weight_kg": 0}}
    assert (await ad_creator.create_draft("Ding", [])).parcel is None
    client.fake.ad_extra = {"parcel": None}
    assert (await ad_creator.create_draft("Ding", [])).parcel is None


async def test_owners_count_survives_a_failed_draft(client, monkeypatch):  # noqa: F811
    async def boom(**kw):
        raise LLMError("kaputt")
    monkeypatch.setattr(client.fake, "complete", boom)
    d = await ad_creator.create_draft("Stühle", [], quantity=4)
    assert d.status == "error" and d.quantity == 4


def _draft(**kw):
    with dbmod.session() as db:
        d = dbmod.AdDraft(input_text="x", title="Stuhl", description="Gut.", price_suggestion=12, **kw)
        db.add(d)
        db.commit()
        db.refresh(d)
    return d


def _get(draft_id):
    with dbmod.session() as db:
        return db.get(dbmod.AdDraft, draft_id)


@pytest.mark.parametrize("sent,want", [("4", 4), ("", None), ("1", None), ("0", None), ("abc", None), ("5000", 999)])
def test_save_fields_quantity(client, sent, want):  # noqa: F811
    d = _draft(quantity=7)
    ad_creator.save_fields(d.id, " Stuhl ", "Text\r\nzwei", "12,5", sent)
    got = _get(d.id)
    assert got.quantity == want and got.title == "Stuhl" and got.price_suggestion == 12.5


def test_save_fields_without_a_count_field_keeps_it(client):  # noqa: F811
    d = _draft(quantity=7)
    ad_creator.save_fields(d.id, "Stuhl", "Text", "12")
    assert _get(d.id).quantity == 7
    ad_creator.update_draft(d.id, refining=True)
    ad_creator.save_fields(d.id, "Stuhl", "Text", "12", "3")             # refused while Claude revises
    assert _get(d.id).quantity == 7


def test_draft_page_and_save_route(client):  # noqa: F811
    d = _draft(quantity=7)
    page = client.get(f"/new-ad/{d.id}").text
    assert 'name="quantity" id="quantity" min="1" max="999" inputmode="numeric" placeholder="1" value="7"' in page
    assert "Pcs." in page and "/myads?ad=" not in page                    # a draft has no ad of its own yet

    client.post(f"/new-ad/{d.id}", data={"title": "Stuhl", "description": "Gut.", "price": "9", "quantity": "3"})
    assert _get(d.id).quantity == 3
    client.post(f"/new-ad/{d.id}", data={"title": "Stuhl", "description": "Gut.", "price": "9"})
    assert _get(d.id).quantity == 3                                       # an older form without the field
    client.post(f"/new-ad/{d.id}", data={"title": "Stuhl", "description": "Gut.", "price": "9", "quantity": ""})
    assert _get(d.id).quantity is None
    assert 'placeholder="1" value=""' in client.get(f"/new-ad/{d.id}").text


def test_batch_table_saves_and_refines_with_the_count(client):  # noqa: F811
    d = _draft(quantity=2)
    with dbmod.session() as db:
        batch = dbmod.AdBatch(status="review", groups=[{"id": "g1", "photos": [], "draft_id": d.id}])
        db.add(batch)
        db.commit()
        db.refresh(batch)
    url = f"/new-ad/batch/{batch.id}/drafts"
    fields = {f"{d.id}.title": "Stuhl", f"{d.id}.description": "Gut.", f"{d.id}.price": "9"}
    client.post(url, data={**fields, f"{d.id}.quantity": "5", "act": "save"})
    assert _get(d.id).quantity == 5
    row = client.get(f"/new-ad/batch/{batch.id}/row/{d.id}").text
    assert f'name="{d.id}.quantity" data-key="quantity" min="1" max="999" inputmode="numeric" placeholder="1" value="5"' in row

    client.fake.ad_extra = {"quantity": 9, "parcel": {"length_cm": 1, "width_cm": 1, "height_cm": 1, "weight_kg": 1}}
    client.post(url, data={**fields, f"{d.id}.title": "Stuhl neu", f"{d.id}.description": "x", f"{d.id}.quantity": "6",
                           f"{d.id}.instruction": "kürzer", "act": f"refine:{d.id}"})
    wait_jobs()
    assert "Stückzahl: 6 (Preis pro Stück)" in client.fake.calls[-1]["prompt"]
    after = _get(d.id)
    assert after.title == "KTM Kinderrad 20 Zoll blau" and after.quantity == 6      # a revision keeps the owner's count
    assert after.parcel is None                                                     # ...and does not invent a parcel
