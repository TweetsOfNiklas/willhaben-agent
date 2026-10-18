"""Selling preferences (Settings → Selling preferences, app/services/selling.py): stored values, the German lines
Claude gets, the e-mail/link guard (willhaben censors both), the shipping preference and the publish handover preset."""
import pytest
from fastapi.testclient import TestClient

from app.db import Conversation, Draft, Shipment, get_setting, session, set_setting
from app.llm import prompts
from app.llm.base import LLMResult
from app.services import ad_creator, chats, my_ads, selling, shipping
from app.services.shipping_tariffs import CARRIERS, options_for
from app.willhaben import insert
from tests.test_auto_reply import env  # noqa: F401  (pytest fixture)
from tests.test_dashboard import client  # noqa: F401  (pytest fixture)
from tests.test_publish_page import _draft, _get, wh  # noqa: F401  (wh: pytest fixture)

PARCEL = {"length_cm": 30, "width_cm": 22, "height_cm": 6, "weight_kg": 1.1}


def _app_client():
    from app.main import app
    return TestClient(app)


# ------------------------------------------------------------------ settings
def test_defaults_and_save_round_trip():
    assert selling.config() == {"handover": "pickup", "shipping_cost": "buyer", "delivery": "any", "carrier": "",
                                "payment": ["transfer", "paylivery", "cash"], "notes": ""}
    selling.save({"sell_handover": "both", "sell_shipping_cost": "included", "sell_delivery": "shop",
                  "sell_carrier": "gls", "sell_payment": ["cash", "transfer", "bitcoin"],
                  "sell_notes": "  Rechnung vorhanden.\r\nNur werktags.  "})
    assert selling.config() == {"handover": "both", "shipping_cost": "included", "delivery": "shop", "carrier": "gls",
                                "payment": ["transfer", "cash"], "notes": "Rechnung vorhanden.\nNur werktags."}
    assert get_setting("sell_payment") == "transfer,cash"
    # values outside the choices keep the stored ones; "" carrier = cheapest; nothing ticked = no payment ways
    selling.save({"sell_handover": "teleport", "sell_shipping_cost": "seller", "sell_delivery": "drone",
                  "sell_carrier": "ups", "sell_notes": ""})
    c = selling.config()
    assert (c["handover"], c["shipping_cost"], c["delivery"], c["carrier"]) == ("both", "included", "shop", "gls")
    assert c["payment"] == [] and c["notes"] == ""
    selling.save({"sell_carrier": ""})
    assert selling.config()["carrier"] == "" and selling.handover() == "both"
    set_setting("sell_handover", "garbage")                          # e.g. an old backup: the default counts
    assert selling.handover() == "pickup" and selling.config()["handover"] == "pickup"


def test_settings_page_renders_and_saves():
    with _app_client() as c:
        page = c.get("/settings/selling").text
        assert "Selling preferences" in page and 'name="section" value="selling"' in page
        assert 'name="sell_handover" value="pickup" checked' in page and 'value="cash" checked' in page
        assert 'href="/settings/rules"' in page and "My rules" in page
        assert selling.CONTACT_RULE in page and "Übergabe: nur Abholung, kein Versand" in page   # the preview
        assert all(f'<option value="{k}"' in page for k in CARRIERS)
        r = c.post("/settings", data={"section": "selling", "sell_handover": "shipping",
                                      "sell_shipping_cost": "included", "sell_delivery": "home", "sell_carrier": "dpd",
                                      "sell_payment": ["transfer", "paylivery"], "sell_notes": "Rechnung vorhanden"},
                   follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/settings/selling?msg=")
        page = c.get("/settings/selling").text
        assert 'name="sell_handover" value="shipping" checked' in page and '<option value="dpd" selected>' in page
        assert 'value="paylivery" checked' in page and 'value="cash" checked' not in page
        assert "Rechnung vorhanden</textarea>" in page
    assert selling.config()["carrier"] == "dpd" and get_setting("reply_rules")   # the rules were not touched


@pytest.mark.parametrize("data", [{"section": "unknown", "reply_rules": ""}, {"reply_rules": "", "auto_draft": "1"}])
def test_unknown_or_missing_section_saves_nothing(data):
    set_setting("reply_rules", "Meine Regel")
    set_setting("auto_draft", "0")
    with _app_client() as c:
        r = c.post("/settings", data=data, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings"
    assert get_setting("reply_rules") == "Meine Regel" and get_setting("auto_draft") == "0"


def test_settings_nav_groups_selling_with_rules():
    with _app_client() as c:
        page = c.get("/settings/rules").text
    nav = page[page.index("<nav"):page.index("</nav>")]
    order = [nav.index(f'href="/settings/{s}"') for s in ("selling", "rules", "claude")]
    assert order == sorted(order)


# ------------------------------------------------------------------ what Claude gets
def test_prompt_block_pickup_only_by_default():
    block = selling.prompt_block()
    assert block.splitlines() == [
        "Meine Verkaufsvorgaben (allgemein; sagen meine Regeln oben, die Anzeige selbst oder meine Angaben zu dieser "
        "Anzeige etwas anderes, gilt das):",
        "- Übergabe: nur Abholung, kein Versand",
        "- Zahlung: Überweisung, bar bei Abholung",          # no PayLivery without shipping
    ]


def test_prompt_block_shipping_included_with_paylivery():
    selling.save({"sell_handover": "both", "sell_shipping_cost": "included", "sell_delivery": "shop",
                  "sell_carrier": "gls", "sell_payment": ["transfer", "paylivery", "cash"],
                  "sell_notes": "Originalverpackung vorhanden."})
    block = selling.prompt_block()
    assert "- Übergabe: Abholung oder Versand" in block
    assert ("- Versandkosten: im Preis inkludiert bei Zustellung an einen Paketshop/eine Paketbox; Zustellung an die "
            "Haustür gegen Aufpreis") in block
    assert "- Bevorzugter Paketdienst: GLS" in block
    assert "- Zahlung: Überweisung, willhaben PayLivery, bar bei Abholung" in block
    assert "- Bei PayLivery zahlt der Käufer den Versand über willhaben; „inklusive“ gilt nur bei Überweisung." in block
    assert block.endswith("Weitere Angaben zum Verkauf:\nOriginalverpackung vorhanden.")
    assert "Erwähne die Übergabe" not in block
    assert selling.prompt_block(for_ad=True).endswith(
        "Erwähne die Übergabe/Versand-Regel am Ende der Beschreibung in einem Satz.")


@pytest.mark.parametrize("form, has, lacks", [
    ({"sell_handover": "shipping", "sell_shipping_cost": "buyer", "sell_delivery": "home"},
     ["- Übergabe: nur Versand, keine Abholung",
      "- Versandkosten: zahlt der Käufer zusätzlich (Zustellung an die Haustür)",
      "- Zahlung: Überweisung, willhaben PayLivery"], ["bar bei Abholung", "Bevorzugter Paketdienst", "inklusive"]),
    ({"sell_handover": "shipping", "sell_shipping_cost": "included", "sell_delivery": "any",
      "sell_payment": ["transfer"]},
     ["- Versandkosten: im Preis inkludiert\n", "- Zahlung: Überweisung"], ["PayLivery"]),
    ({"sell_handover": "both", "sell_shipping_cost": "included", "sell_delivery": "home", "sell_payment": []},
     ["- Versandkosten: im Preis inkludiert (Zustellung an die Haustür)"], ["Zahlung:"]),
    ({"sell_handover": "both", "sell_shipping_cost": "buyer", "sell_delivery": "shop", "sell_carrier": "hermes"},
     ["- Versandkosten: zahlt der Käufer zusätzlich (bevorzugt Zustellung an einen Paketshop/eine Paketbox)",
      "- Bevorzugter Paketdienst: Hermes"], ["inklusive"]),
    ({"sell_handover": "pickup", "sell_shipping_cost": "included", "sell_carrier": "gls"},
     ["- Übergabe: nur Abholung, kein Versand"], ["Versandkosten", "Paketdienst", "PayLivery"]),
])
def test_prompt_block_wording(form, has, lacks):
    selling.save({"sell_payment": ["transfer", "paylivery", "cash"], **form})
    block = selling.prompt_block() + "\n"
    for s in has:
        assert s in block, s
    for s in lacks:
        assert s not in block, s


def test_append_to_rules():
    assert selling.append_to("") == selling.prompt_block()
    assert selling.append_to("- Du-Form\n") == f"- Du-Form\n\n{selling.prompt_block()}"


def test_contact_rule_is_the_last_bullet_of_reply_and_ad_prompts():
    bullet = f"- {selling.CONTACT_RULE}\n"
    assert prompts.REPLY_SYSTEM.endswith(bullet) and prompts.AD_SYSTEM.endswith(bullet)
    assert bullet in prompts.AUTO_REPLY_SYSTEM and bullet in prompts.PURCHASE_SYSTEM
    assert "E-Mail-Adressen und Links" in selling.CONTACT_RULE


async def test_seller_drafts_get_the_block_buyer_drafts_do_not(env):  # noqa: F811
    backend, account, notes = env
    selling.save({"sell_handover": "both", "sell_carrier": "gls"})
    await chats.draft_reply("c1")
    call = backend.calls[-1]
    assert "Meine Verkaufsvorgaben" in call["prompt"] and "Bevorzugter Paketdienst: GLS" in call["prompt"]
    assert selling.CONTACT_RULE in call["system"]
    await chats.draft_reply("c1", auto=True)
    assert "Bevorzugter Paketdienst: GLS" in backend.calls[-1]["prompt"]
    assert selling.CONTACT_RULE in backend.calls[-1]["system"]
    with session() as db:
        conv = db.get(Conversation, "c1")
        conv.role = "buyer"
        db.add(conv)
        db.commit()
    await chats.draft_reply("c1")                     # a manual draft as buyer, without a purchase order
    assert "Meine Verkaufsvorgaben" not in backend.calls[-1]["prompt"]


class _AdBackend:
    model = "fake"

    def __init__(self):
        self.calls = []

    async def complete(self, *, system, prompt, schema, images=None):
        self.calls.append({"system": system, "prompt": prompt})
        return LLMResult(data={"title": "Hocker", "description": "Ein Hocker.", "category_hint": "",
                               "condition": "gebraucht", "attributes": [], "search_keywords": "",
                               "price_estimate": None, "product_query": "",
                               "quantity": 1, "parcel": {"length_cm": 0, "width_cm": 0, "height_cm": 0, "weight_kg": 0},
                               "note": ""}, model="fake")


async def test_ad_prompts_get_the_block(monkeypatch):
    from app.willhaben.public import WillhabenError

    backend = _AdBackend()
    monkeypatch.setattr(ad_creator, "get_backend", lambda *a: backend)
    monkeypatch.setattr("app.llm.base.get_backend", lambda *a: backend)
    set_setting("reply_rules", "- Du-Form")
    selling.save({"sell_handover": "shipping", "sell_shipping_cost": "included"})
    d = await ad_creator.create_draft("Hocker aus Holz", [])
    await ad_creator.refine_draft(d.id, "kürzer")

    class _NoDetail:
        async def get_detail(self, ad_id):
            raise WillhabenError("offline")

    monkeypatch.setattr(my_ads, "get_public", lambda: _NoDetail())
    await my_ads.ai_revise("555", "Hocker", "Ein Hocker.", 20.0, "kürzer")
    assert len(backend.calls) == 3
    for call in backend.calls:
        assert "- Du-Form\n\nMeine Verkaufsvorgaben" in call["prompt"]
        assert "Versandkosten: im Preis inkludiert" in call["prompt"]
        assert "Erwähne die Übergabe/Versand-Regel am Ende der Beschreibung in einem Satz." in call["prompt"]
        assert call["system"].endswith(f"- {selling.CONTACT_RULE}\n")


# ------------------------------------------------------------------ e-mail addresses and links
@pytest.mark.parametrize("text, hits", [
    ("Schreib mir an max.mustermann@gmx.at.", ["max.mustermann@gmx.at"]),
    ("max (at) gmx (punkt) at", ["max (at) gmx (punkt) at"]),
    ("Fotos: https://willhaben.at/iad/123.", ["https://willhaben.at/iad/123"]),
    ("auf www.meinshop.at/x", ["www.meinshop.at/x"]),
    ("Infos auf meinshop.de/artikel?x=1 oder example.com", ["meinshop.de/artikel?x=1", "example.com"]),
    ("wa.me/43660123 oder t.me/max", ["wa.me/43660123", "t.me/max"]),
    ("Willhaben.AT und willhaben.at", ["Willhaben.AT", "willhaben.at"]),
    ("Mail: a@b.at, nochmal a@b.at", ["a@b.at"]),
])
def test_contact_hits(text, hits):
    assert selling.contact_hits(text) == hits


@pytest.mark.parametrize("text", [
    "Preis 4,50 €, Versand 4.50 €", "Art.-Nr. 06019H5202", "Bosch GSR 18V-55 06019H5202", "z.B. das Netzteil",
    "ca. 30 cm, ca.30cm", "s.Oliver Jacke Gr.XL", "B.Braun Thermometer", "Ja.gerne, bis morgen", "Wi-Fi 6 (802.11ax)",
    "1.5kg, Nr.5, u.a. Kabel", "Ja, ist noch da. Abholung in Wien 15.", "Tel. 0660 1234567", "", None,
])
def test_no_contact_hits(text):
    assert selling.contact_hits(text) == []


async def test_auto_reply_held_back_on_an_email(env):  # noqa: F811
    backend, account, notes = env
    backend.data = {"reply": "Schreib mir an max@gmx.at, dann schick ich Fotos."}
    d = await chats.auto_reply("c1")
    assert d.status == "draft" and not account.sent
    assert "Answer contains an e-mail address or link – willhaben censors them" in d.auto_reason


async def test_publish_refused_on_a_link(wh):  # noqa: F811
    d = _draft()
    ad_creator.update_draft(d.id, description="Gut erhalten. Mehr Fotos auf www.meinshop.at/hocker")
    await ad_creator.prepare_willhaben(d.id)
    with pytest.raises(insert.InsertError, match="„www.meinshop.at/hocker“"):
        await ad_creator.publish_willhaben(d.id, 0, {})
    assert not ad_creator.enqueue_publish(d.id, 0, {})
    assert "www.meinshop.at/hocker" in _get(d.id).error and wh.published == []
    ad_creator.update_draft(d.id, title="Hocker – Infos auf hocker.at", description="Gut erhalten.")
    with pytest.raises(insert.InsertError, match="hocker.at"):
        await ad_creator.publish_willhaben(d.id, 0, {})
    ad_creator.update_draft(d.id, title="Hocker")
    await ad_creator.publish_willhaben(d.id, 0, {})
    assert len(wh.published) == 1


def test_composer_warns_before_sending_a_link():
    with session() as db:
        db.add(Conversation(id="c9", ad_id="9", ad_title="Hocker", partner_name="Anna", role="seller"))
        db.add(Draft(conversation_id="c9", text="Mehr Fotos auf www.meinshop.at, schreib mir an max@gmx.at"))
        db.commit()
    with _app_client() as c:
        page = c.get("/chats/c9").text
    check = page[page.index("data-draft-check"):page.index('name="text"')]
    assert "Check before sending" in check
    assert "willhaben censors e-mail addresses and links – remove: www.meinshop.at, max@gmx.at" in check
    with session() as db:
        d = db.get(Draft, 1)
        d.text = "Ja, ist noch da."
        db.add(d)
        db.commit()
    with _app_client() as c:
        page = c.get("/chats/c9").text
    assert "data-draft-check" not in page and "data-contact-warn" not in page


# ------------------------------------------------------------------ shipping preference
def test_quote_keeps_price_order_without_preference():
    assert shipping.quote(PARCEL) == options_for(PARCEL, list(CARRIERS))


def test_quote_puts_the_preferred_carrier_and_delivery_first():
    selling.save({"sell_delivery": "home"})
    opts = shipping.quote(PARCEL)
    kinds = [o.delivery for o in opts]
    assert kinds == sorted(kinds, key=lambda k: k != "home")          # every home option before the shop ones
    home = [o for o in opts if o.delivery == "home"]
    assert [o.price for o in home] == sorted(o.price for o in home)  # cheapest first within the preference
    selling.save({"sell_delivery": "home", "sell_carrier": "dpd"})
    opts = shipping.quote(PARCEL)
    assert [o.key for o in opts[:2]] == ["dpd:S:home", "dpd:S:shop"] and opts[2].delivery == "home"
    selling.save({"sell_delivery": "any", "sell_carrier": "gls"})
    assert {o.carrier for o in shipping.quote(PARCEL)[:2]} == {"gls"}
    assert selling.preference_text() == "GLS"


def test_auto_option_follows_the_preference():
    with session() as db:
        db.add(Conversation(id="c5", ad_id="5", role="seller"))
        db.add(Shipment(conversation_id="c5", parcel=PARCEL, recipient={"name": "Max", "zip": "1150", "city": "Wien"}))
        db.commit()
    assert shipping._auto_option(shipping.get(1)).option == "gls:S:shop"        # no preference: the cheapest online one
    assert shipping.offer_text(shipping.get(1), None)[1].startswith("Cheapest: GLS S → Shop")
    selling.save({"sell_carrier": "dpd", "sell_delivery": "home"})
    shipping.update(1, option=None)
    s = shipping._auto_option(shipping.get(1))
    assert s.option == "dpd:S:home" and s.price == 4.70
    assert shipping.offer_text(s, None)[1].startswith("Preferred: DPD S")
    assert shipping._auto_option(s).option == "dpd:S:home"                      # a chosen option that fits stays


async def test_shipping_card_shows_the_preference(monkeypatch):
    from app.services import dropoff

    async def nowhere(*a, **kw):
        return {}

    monkeypatch.setattr(dropoff, "nearest", nowhere)
    with session() as db:
        db.add(Conversation(id="c6", ad_id="6", ad_title="Buch", partner_name="Max", role="seller"))
        db.add(Shipment(conversation_id="c6", parcel=PARCEL, recipient={"name": "Max", "zip": "1150", "city": "Wien"}))
        db.commit()
    with _app_client() as c:
        assert "data-ship-pref" not in c.get("/chats/c6").text
        rows = c.get("/chats/c6").text.split('id="shipping"')[1]
        assert rows.index("GLS S → Shop") < rows.index("DPD S")                 # cheapest first
        selling.save({"sell_carrier": "dpd", "sell_delivery": "home"})
        page = c.get("/chats/c6").text
    assert "Preference: DPD, door" in page and 'href="/settings/selling"' in page
    rows = page.split('id="shipping"')[1]
    assert rows.index("DPD S<") < rows.index("DPD S → Shop") < rows.index("GLS S → Shop")


# ------------------------------------------------------------------ handover preset of the publish form
def test_publish_form_starts_with_the_usual_handover(client, wh):  # noqa: F811
    d = _draft()
    client.post(f"/new-ad/{d.id}/willhaben/prepare", data={})
    assert 'name="handover" value="pickup" checked' in client.get(f"/new-ad/{d.id}").text
    set_setting("sell_handover", "both")
    page = client.get(f"/new-ad/{d.id}").text
    assert 'name="handover" value="both" checked' in page and 'name="handover" value="pickup" checked' not in page
    ad_creator.update_draft(d.id, wh_plan={**_get(d.id).wh_plan, "shipping": {"handover": "shipping"}})
    assert 'name="handover" value="shipping" checked' in client.get(f"/new-ad/{d.id}").text   # the draft's own wins


async def test_publish_without_handover_uses_the_usual_one(wh):  # noqa: F811
    assert ad_creator.parse_shipping({}) == {"handover": "pickup"}
    set_setting("sell_handover", "both")
    assert ad_creator.parse_shipping({}) == {"handover": "both", "paylivery": False, "size": None, "carrier": None}
    assert ad_creator.parse_shipping({"handover": "pickup"}) == {"handover": "pickup"}
    d = _draft()
    await ad_creator.prepare_willhaben(d.id)
    await ad_creator.publish_willhaben(d.id, 0, {})
    assert wh.published[-1]["shipping"]["handover"] == "both"
    assert _get(d.id).status == "published"


def test_bugreport_keeps_the_choices_and_hides_the_notes():
    from app.services import bugreport

    selling.save({"sell_handover": "both", "sell_carrier": "gls", "sell_payment": ["transfer", "cash"],
                  "sell_notes": "Abholung bei Max Mustermann, Musterstraße 1"})
    app_settings = bugreport.build(include_settings=True)["settings"]["app"]
    assert app_settings["sell_handover"] == "both" and app_settings["sell_carrier"] == "gls"
    assert app_settings["sell_payment"] == "transfer,cash" and app_settings["sell_notes"] == "<set>"



async def test_seller_drafts_get_the_style_profile_and_the_other_chats(env):  # noqa: F811
    """The profile from Settings → My rules → Writing style and my messages to the ad's other buyers reach every draft
    of my own ads; a purchase order's drafts keep their own persona (no profile)."""
    from app.db import utcnow
    from app.services.chats import _upsert_message

    backend, account, notes = env
    set_setting("reply_style", "- kleingeschrieben, kein gruß")
    with session() as db:
        db.add(Conversation(id="c2", ad_id="111", ad_title="Logitech S315i", partner_name="Ben", role="seller"))
        _upsert_message(db, "c2", "x1", "out", "ja funktioniert, netzteil ist dabei", utcnow())
        db.commit()
    await chats.draft_reply("c1")
    p = backend.calls[-1]["prompt"]
    assert "Mein Schreibstil" in p and "- kleingeschrieben, kein gruß" in p
    assert "Was ich anderen Interessenten zu dieser Anzeige geschrieben habe" in p and "- ja funktioniert, netzteil ist dabei" in p
    await chats.draft_reply("c1", auto=True)
    assert "Mein Schreibstil" in backend.calls[-1]["prompt"] and "- ja funktioniert, netzteil ist dabei" in backend.calls[-1]["prompt"]
    with session() as db:
        conv = db.get(Conversation, "c1")
        conv.role = "buyer"
        db.add(conv)
        db.commit()
    await chats.draft_reply("c1")                     # as buyer (no purchase order): my style, nobody else's chats
    p = backend.calls[-1]["prompt"]
    assert "Mein Schreibstil" in p and "Was ich anderen Interessenten" not in p
    from app.db import Purchase
    with session() as db:                             # a purchase order: Claude's buyer persona, not my style
        db.add(Purchase(ad_id="999", conversation_id="c1", mode="pickup", status="negotiating", max_price=250.0,
                        asking_price=300.0))
        db.commit()
    for auto in (False, True):
        await chats.draft_reply("c1", auto=auto)
        p = backend.calls[-1]["prompt"]
        assert "Mein Schreibstil" not in p and "Was ich anderen Interessenten" not in p and "Höchstpreis: 250 €" in p
