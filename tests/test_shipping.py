import pytest

from app.db import Conversation, Message, MyAd, session, utcnow
from app.llm.base import LLMError, LLMResult
from app.services import label_fill, shipping
from app.services.shipping_tariffs import CARRIERS, options_for

ADDR = {"name": "Max Muster", "street": "Hauptstraße", "house_no": "12", "extra": "3/4", "zip": "1150",
        "city": "Wien", "phone": "", "email": ""}


def test_tariffs_fit_and_sort():
    small = {"length_cm": 30, "width_cm": 20, "height_cm": 3, "weight_kg": 0.8}
    opts = options_for(small, list(CARRIERS))
    keys = [o.key for o in opts]
    assert keys[0] == "post:Päckchen S:home" and opts[0].price == 3.10 and not opts[0].online
    assert options_for({**small, "height_cm": 4}, list(CARRIERS))[0].key == "gls:XS:shop"   # too thick for Päckchen S
    assert "gls:XS:shop" in keys and "gls:S:home" not in keys          # only the smallest fitting size per carrier
    assert [o.price for o in opts] == sorted(o.price for o in opts)
    big = {"length_cm": 70, "width_cm": 50, "height_cm": 40, "weight_kg": 12}
    big_keys = {o.key for o in options_for(big, list(CARRIERS))}
    assert "hermes:L:home" in big_keys                                 # 70 + 40 = 110 ≤ 120
    assert not any(k.startswith("post:PM") for k in big_keys)          # Paketmarke only up to 10 kg
    assert "gls:XL:home" in big_keys and "dpd:XL:home" in big_keys
    assert options_for({"length_cm": 210, "width_cm": 90, "height_cm": 70, "weight_kg": 35}, list(CARRIERS)) == []


def test_clean_address_and_parcel():
    assert shipping.clean_address({**ADDR, "zip": "A-1150"})["zip"] == "1150"
    assert shipping.clean_address({**ADDR, "street": ""}) is None
    p = shipping.clean_parcel({"length_cm": 10, "width_cm": 40.4, "height_cm": "20", "weight_kg": 1.26})
    assert (p["length_cm"], p["width_cm"], p["height_cm"], p["weight_kg"]) == (40, 20, 10, 1.3)
    assert shipping.clean_parcel({"length_cm": 0, "weight_kg": 1}) is None


def test_address_hint():
    assert shipping.looks_like_address("Bitte an Max Muster, Hauptstraße 12/3, 1150 Wien")
    assert shipping.looks_like_address("Kirchengasse 5")
    assert not shipping.looks_like_address("Ist das noch da?")


@pytest.fixture
def chat(monkeypatch):
    class Backend:
        model = "fake"
        calls = []

        async def complete(self, *, system, prompt, schema, images=None):
            self.calls.append(prompt)
            return LLMResult(data={"found": True, "address": ADDR, "note": "",
                                   "parcel": {"length_cm": 30, "width_cm": 22, "height_cm": 6, "weight_kg": 1.1,
                                              "contents": "Buch"}}, model="fake")

    backend = Backend()
    monkeypatch.setattr("app.llm.base.get_backend", lambda *a: backend)

    async def no_ctx(cid):
        with session() as db:
            return db.get(Conversation, cid)

    monkeypatch.setattr("app.services.chats.ensure_ad_context", no_ctx)
    with session() as db:
        db.add(MyAd(ad_id="77", heading="Kinderbuch"))
        db.add(Conversation(id="c1", ad_id="77", ad_title="Kinderbuch", partner_name="Max", role="seller",
                            last_message_text="Max Muster, Hauptstraße 12/3/4, 1150 Wien"))
        db.add(Message(conversation_id="c1", ext_id="m1", direction="in",
                       text="Max Muster, Hauptstraße 12/3/4, 1150 Wien", created_at=utcnow()))
        db.commit()
    return backend


async def test_detect_creates_shipment_and_remembers_parcel(chat):
    s = await shipping.detect("c1")
    assert s.recipient["street"] == "Hauptstraße" and s.parcel["weight_kg"] == 1.1 and s.parcel["source"] == "claude"
    assert s.option == "gls:S:shop"                                 # 30 + 6 = 36 > XS; cheapest online option
    s = shipping.set_parcel(s.id, {"length_cm": 30, "width_cm": 22, "height_cm": 8, "weight_kg": 1.4})
    with session() as db:
        assert db.get(MyAd, "77").parcel["height_cm"] == 8           # remembered for the next buyer
    assert shipping.by_key(s.option).fits(s.parcel)
    s2 = await shipping.detect("c1")                                 # same chat: updates the draft, keeps the parcel
    assert s2.id == s.id and s2.parcel["height_cm"] == 8
    shipping.choose(s.id, "gls:S:shop")
    assert shipping.get(s.id).price == 3.90
    with pytest.raises(shipping.ShippingError):
        shipping.choose(s.id, "gls:XS:home")                         # does not fit


async def test_incoming_address_pushes_offer(chat, monkeypatch):
    notes = []

    async def fake_notify(kind, title, message, **kw):
        notes.append((kind, title, message, kw))

    monkeypatch.setattr("app.services.notify.notify", fake_notify)
    shipping.save_sender({**ADDR, "name": "Max"})
    with session() as db:
        conv = db.get(Conversation, "c1")
    await shipping.on_incoming(conv)
    for task in list(shipping._tasks):
        await task
    kind, title, message, kw = notes[-1]
    assert kind == "shipping" and "Max" in title and "1150 Wien" in message
    assert kw["actions"][0]["action"].startswith("WH_SHIP_")
    conv.role = "buyer"                                              # only when I sell
    await shipping.on_incoming(conv)
    assert not shipping._tasks


async def test_ship_push_button_opens_form(chat, monkeypatch):
    from app.services import ha_actions

    started, notes = [], []

    async def fake_start(sid):
        started.append(sid)
        return "5 fields filled"

    async def fake_notify(kind, title, message, **kw):
        notes.append((kind, title, kw.get("url")))

    async def fake_clear(tag):
        pass

    monkeypatch.setattr(label_fill, "start", fake_start)
    monkeypatch.setattr("app.services.notify.notify", fake_notify)
    monkeypatch.setattr("app.services.notify.clear_push", fake_clear)
    s = await shipping.detect("c1")
    act = ha_actions.ship_actions(s.id)[0]["action"]
    assert await ha_actions.handle({"action": act.replace(act[-12:], "0" * 12)}) is None   # bad signature
    assert await ha_actions.handle({"action": act}) == "label form"
    assert started == [s.id] and notes[-1][2].endswith("/session")
    shipping.update(s.id, status="labelled")
    assert await ha_actions.handle({"action": act}) is None          # only drafts


async def test_label_start_needs_sender_and_online(chat):
    s = await shipping.detect("c1")
    with pytest.raises(shipping.ShippingError, match="sender"):
        await label_fill.start(s.id)
    shipping.save_sender({**ADDR, "name": "Max"})
    shipping.update(s.id, option="post:Päckchen S:home")
    with pytest.raises(shipping.ShippingError, match="counter"):     # Päckchen: no online label
        await label_fill.start(s.id)


def test_heuristic_mapping():
    data = {"absender": {**ADDR, "name": "Max Muster", "city": "Graz"},
            "empfaenger": ADDR, "paket": {}}
    fields = [
        {"idx": 1, "tag": "input", "type": "text", "label": "Vorname", "name": "sender_first", "value": ""},
        {"idx": 2, "tag": "input", "type": "text", "label": "PLZ", "name": "s_zip", "value": ""},
        {"idx": 3, "tag": "input", "type": "text", "label": "Empfänger Nachname", "name": "r_last", "value": ""},
        {"idx": 4, "tag": "input", "type": "text", "label": "Ort", "name": "r_city", "value": ""},
        {"idx": 5, "tag": "input", "type": "checkbox", "label": "Ich akzeptiere die AGB", "name": "agb", "value": ""},
    ]
    got = {f["idx"]: f["value"] for f in label_fill._heuristic(data, fields)}
    assert got == {1: "Max", 2: "1150", 3: "Muster", 4: "Wien"}


FORM = """<html><body><form>
 <h3>Absender</h3><label for=a>Name</label><input id=a name=sender_name>
 <h3>Empfänger</h3><label>Name <input name=rcpt_name></label><label>PLZ <input name=rcpt_zip></label>
 <label><input type=radio name=size value=S> Größe S</label><label><input type=radio name=size value=M> Größe M</label>
 <label><input type=checkbox name=agb> Ich akzeptiere die AGB</label>
 <select name=country><option value=DE>Deutschland</option><option value=AT>Österreich</option></select>
 <button type=submit onclick="window.__clicked=1">Jetzt kaufen</button>
</form></body></html>"""


async def test_collect_and_fill_in_real_browser():
    pw_mod = pytest.importorskip("playwright.async_api")
    async with pw_mod.async_playwright() as pw:
        try:
            browser = await pw.chromium.launch()
        except Exception as e:  # noqa: BLE001 - no browser in this environment
            pytest.skip(f"no chromium: {e}")
        page = await browser.new_page()
        await page.set_content(FORM)
        fields, where = await label_fill._collect(page)
        by_name = {f["name"]: f for f in fields}
        assert by_name["sender_name"]["label"] == "Name" and "Österreich = AT" in by_name["country"]["options"]
        radio_m = next(f for f in fields if f["type"] == "radio" and "M" in f["label"])
        fills = [{"idx": by_name["sender_name"]["idx"], "value": "Max"},
                 {"idx": by_name["rcpt_zip"]["idx"], "value": "1150"},
                 {"idx": radio_m["idx"], "value": "check"},
                 {"idx": by_name["agb"]["idx"], "value": "check"},          # must be refused
                 {"idx": by_name["country"]["idx"], "value": "AT"}]
        n = await label_fill._apply(fields, where, fills)
        assert n == 4
        assert await page.input_value("[name=rcpt_zip]") == "1150"
        assert await page.is_checked("input[name=size][value=M]")
        assert not await page.is_checked("[name=agb]")
        assert await page.evaluate("window.__clicked") is None             # nothing was clicked
        assert await page.input_value("[name=country]") == "AT"
        await browser.close()


async def test_fill_falls_back_without_claude(monkeypatch):
    class Broken:
        model = "x"

        async def complete(self, **kw):
            raise LLMError("down")

    monkeypatch.setattr("app.llm.base.get_backend", lambda *a: Broken())
    with pytest.raises(shipping.ShippingError):
        await label_fill.fill_current()                                     # nothing open
