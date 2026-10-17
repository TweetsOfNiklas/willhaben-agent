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
    backend.purposes = []                           # the get_backend(purpose) of each call
    monkeypatch.setattr("app.llm.base.get_backend", lambda purpose=None: backend.purposes.append(purpose) or backend)

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
    assert chat.purposes == ["shipping"]                             # Settings → Claude → "Shipping & labels"
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
    assert kind == "shipping" and title == "Address received – not paid yet"
    assert "Max Muster" in message and "1150 Wien" in message
    assert [a["action"][:8] for a in kw["actions"]] == ["WH_PAID_", "WH_SHIP_"]
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
        notes.append((kind, title, kw.get("url"), kw.get("tag")))

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
    assert notes[-1][3] == f"wh-ship-{s.id}"                           # same push as "address received"
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


async def test_chat_page_renders_with_shipment(chat):
    from fastapi.testclient import TestClient

    from app.main import app
    s = await shipping.detect("c1")
    assert s.option
    with TestClient(app) as c:
        r = c.get("/chats/c1")
        assert r.status_code == 200 and 'id="shipping"' in r.text and "Create label" in r.text
        assert "All options" in r.text and ("too heavy:" in r.text or "too big:" in r.text or "too long:" in r.text)
        r = c.post(f"/shipments/{s.id}", data={**ADDR, "length_cm": 30, "width_cm": 22, "height_cm": 8,
                                                "weight_kg": 1.4}, follow_redirects=False)
        assert r.status_code == 303
        assert c.get("/settings").status_code == 200


@pytest.fixture
def finders(monkeypatch):
    import httpx

    from app.services import dropoff

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "nominatim.openstreetmap.org":
            return httpx.Response(200, json=[{"lat": "48.2100", "lon": "16.3300"}])
        if host == "api.post.at":
            return httpx.Response(200, json={"data": {"branches": [
                {"type": "ABHOLSTATION", "name": "Abholstation nur Abholung", "openinghourformatted": "",
                 "address": {"streetname": "A", "streetnumber": "1", "postalcode": "1160", "city": "Wien",
                             "coordinates": {"latitudewgs84": 48.2101, "longitudewgs84": 16.3301}},
                 "services": [{"serviceid": 379}]},
                {"type": "FILIALE", "name": "Postfiliale 1160", "openinghourformatted": "Mo-Fr 8-18",
                 "address": {"streetname": "B", "streetnumber": "2", "postalcode": "1160", "city": "Wien",
                             "coordinates": {"latitudewgs84": 48.2200, "longitudewgs84": 16.3300}},
                 "services": [{"serviceid": 1}]}]}})
        if host == "api.gls-group.net":
            assert request.headers["apikey"]
            return httpx.Response(200, json=[
                {"name": "Trafik", "type": "SHOPINSHOP", "street": "C", "houseNumber": "3", "zipCode": "1160",
                 "city": "Wien", "latitude": 48.2105, "longitude": 16.3300, "offersPrepaidParcelDropOff": True,
                 "parcelShopId": "AT-1001"},
                {"name": "Nur Abholung", "type": "LOCKER", "street": "D", "houseNumber": "4", "zipCode": "1160",
                 "city": "Wien", "latitude": 48.2100, "longitude": 16.3300, "offersPrepaidParcelDropOff": False,
                 "parcelShopId": "AT-1002"}])
        if host == "www.mydpd.at":
            return httpx.Response(403, text="captcha")                         # bot protection: no DPD data
        if host == "wsfinder.hlg.at":
            return httpx.Response(200, json=[{"description": "Copyshop", "street": "E", "houseNumber": "5",
                                              "postCode": "1160", "city": "Wien", "lat": 48.2300, "lng": 16.3300}])
        return httpx.Response(404)

    monkeypatch.setattr(dropoff, "_transport", httpx.MockTransport(handler))
    dropoff._cache.clear()
    return dropoff


async def test_nearest_dropoff_points(finders):
    res = await finders.nearest({"street": "Musterstraße", "house_no": "1", "zip": "1160", "city": "Wien"},
                                ["post", "gls", "dpd", "hermes"])
    assert [p["name"] for p in res["post"]] == ["Postfiliale 1160"]           # pickup-only station skipped
    assert [p["name"] for p in res["gls"]] == ["Trafik"] and res["gls"][0]["dist_m"] == 55
    assert res["dpd"] == [] and res["hermes"][0]["dist_m"] > 2000
    assert finders.fmt_dist(55) == "55 m" and finders.fmt_dist(2224) == "2,2 km"
    assert await finders.nearest({"street": "", "zip": ""}, ["post"]) == {}


async def test_delivery_lookup_lists_every_shop_and_keeps_the_sender_geo(finders):
    """A buyer's "→ Shop" target: lockers and shops that take no drop-offs count too, the carrier's id comes along,
    and the buyer's geocode never replaces the sender's cached one."""
    from app.db import get_setting
    buyer = {"street": "Hauptstraße", "house_no": "12", "zip": "1150", "city": "Wien"}
    res = await finders.nearest(buyer, ["gls"], per_carrier=5, purpose="delivery")
    assert [(p["name"], p["id"]) for p in res["gls"]] == [("Nur Abholung", "AT-1002"), ("Trafik", "AT-1001")]
    assert get_setting("ship_sender_geo") == ""                                  # nothing written for a buyer
    assert [p["name"] for p in (await finders.nearest(buyer, ["gls"]))["gls"]] == ["Trafik"]   # drop-off: as before
    assert "1150 Wien" in get_setting("ship_sender_geo")                          # the sender's lookup does cache
    with pytest.raises(ValueError):
        await finders.nearest(buyer, ["gls"], purpose="pickup")


async def test_shop_option_gets_a_parcel_shop_for_the_buyer(chat, finders):
    """GLS S → Shop: the nearest shop is stored right before the form is filled, the owner may pick another, the pick
    survives a re-read of the same address and a door option, and goes to the form filler as empfaenger.paketshop."""
    s = await shipping.detect("c1")
    assert s.option == "gls:S:shop" and shipping.shop_for(s) is None
    shops = await shipping.shops_near(s)
    assert [(sh["name"], sh["carrier"], sh["id"]) for sh in shops] == [("Nur Abholung", "gls", "AT-1002"),
                                                                       ("Trafik", "gls", "AT-1001")]
    assert shops[0]["dist_m"] < shops[1]["dist_m"]
    assert await shipping.ensure_shop(s.id) == {"carrier": "gls", "name": "Nur Abholung", "address": "D 4, 1160 Wien",
                                                "id": "AT-1002"}
    assert shipping.shop_for(shipping.get(s.id))["name"] == "Nur Abholung"
    data = label_fill._data_for(shipping.get(s.id))
    assert data["empfaenger"]["paketshop"] == {"name": "Nur Abholung", "address": "D 4, 1160 Wien", "id": "AT-1002"}
    assert "shop" not in data["empfaenger"] and data["empfaenger"]["name"] == "Max Muster"
    assert data["paket"]["zustellung"] == "shop"

    shipping.set_shop(s.id, {"name": "Trafik", "address": "C 3, 1160 Wien", "id": "AT-1001", "dist_m": 55})
    assert shipping.shop_for(shipping.get(s.id)) == {"carrier": "gls", "name": "Trafik", "address": "C 3, 1160 Wien",
                                                     "id": "AT-1001"}
    assert await shipping.ensure_shop(s.id) == shipping.shop_for(shipping.get(s.id))   # the pick stays
    s = await shipping.detect("c1")                                                 # Claude re-reads the same address
    assert shipping.shop_for(s)["name"] == "Trafik"
    shipping.choose(s.id, "gls:S:home")
    s = shipping.get(s.id)
    assert shipping.shop_for(s) is None and "paketshop" not in label_fill._data_for(s)["empfaenger"]
    assert await shipping.shops_near(s) == [] and await shipping.ensure_shop(s.id) is None
    with pytest.raises(shipping.ShippingError, match="door"):
        shipping.set_shop(s.id, {"name": "Trafik", "address": "C 3, 1160 Wien"})
    shipping.choose(s.id, "dpd:S:shop")                                             # another carrier: its own shop
    assert shipping.shop_for(shipping.get(s.id)) is None
    shipping.choose(s.id, "gls:S:shop")
    assert shipping.shop_for(shipping.get(s.id))["name"] == "Trafik"
    with pytest.raises(shipping.ShippingError, match="incomplete"):
        shipping.set_shop(s.id, {"name": "", "address": "C 3"})
    shipping.set_recipient(s.id, {**ADDR, "house_no": "14"})                       # same postcode and city: kept
    assert shipping.shop_for(shipping.get(s.id))["name"] == "Trafik"
    shipping.set_recipient(s.id, {**ADDR, "zip": "8010", "city": "Graz"})          # moved: the shop no longer fits
    assert shipping.shop_for(shipping.get(s.id)) is None


async def test_chat_page_offers_the_buyers_parcel_shops(chat, finders):
    from fastapi.testclient import TestClient

    from app.main import app
    shipping.save_sender({**ADDR, "name": "Max Muster"})
    s = await shipping.detect("c1")
    with TestClient(app) as c:
        page = c.get("/chats/c1").text
        card = page.split("data-ship-shop")[1].split("</div>")[0]
        assert "Parcel shop for the buyer:" in card and "<strong>Nur Abholung</strong>" in card
        assert "the nearest – picked unless you choose another" in card
        assert 'action="/shipments/%d/shop"' % s.id in card and card.count("<option") == 2
        assert "Shop delivery is never bought automatically yet" in card
        assert shipping.shop_for(shipping.get(s.id)) is None                        # a page view stores nothing
        r = c.post(f"/shipments/{s.id}/shop", data={"shop": '{"name": "Trafik", "address": "C 3, 1160 Wien", "id": "AT-1001"}'},
                   follow_redirects=False)
        assert r.status_code == 303 and "Parcel%20shop%20saved" in r.headers["location"]
        assert shipping.shop_for(shipping.get(s.id))["id"] == "AT-1001"
        card = c.get("/chats/c1").text.split("data-ship-shop")[1].split("</div>")[0]
        assert "<strong>Trafik</strong>" in card and "picked unless" not in card
        assert "selected>Trafik, C 3, 1160 Wien" in card
        r = c.post(f"/shipments/{s.id}/shop", data={"shop": "not json"}, follow_redirects=False)
        assert r.status_code == 303 and "incomplete" in r.headers["location"]
        c.post(f"/shipments/{s.id}/option", data={"key": "gls:S:home"})
        assert "data-ship-shop" not in c.get("/chats/c1").text


async def test_chat_page_sorted_by_nearest(chat, finders):
    from fastapi.testclient import TestClient

    from app.main import app
    shipping.save_sender({**ADDR, "name": "Max Muster"})
    await shipping.detect("c1")
    with TestClient(app) as c:
        near = c.get("/chats/c1?sort=near").text
        cheap = c.get("/chats/c1?sort=price").text
    assert "Trafik" in near and "55 m" in near
    first = lambda html: html.split("data-opt")[1][:400]   # noqa: E731  the first option row (marked data-opt)
    assert "GLS" in first(near)                                               # 55 m beats Post (1.1 km)
    assert "GLS" in first(cheap)                                              # GLS S → Shop is also cheapest


def test_remote_overlay_and_embed():
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as c:
        page = c.get("/chats").text
        assert 'id="remote-overlay"' in page and 'src="about:blank"' in page    # iframe loads only when opened
        embed = c.get("/session?embed=1").text
        assert 'data-embed' in embed and 'nav class="top"' not in embed and 'id="remote-overlay"' not in embed
        assert 'value="/session?embed=1"' in embed or "Browser" in embed          # forms stay inside the overlay


def test_carrier_logins_are_private(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    monkeypatch.setattr(shipping, "_logins_path", lambda: tmp_path / "carrier-logins.json")
    shipping.save_login("gls", "max@example.com", "geheim-123")
    shipping.save_login("post", "max@example.com", "")
    assert (tmp_path / "carrier-logins.json").stat().st_mode & 0o777 == 0o600
    assert shipping.login_status() == {"gls": {"user": "max@example.com", "has_password": True},
                                       "post": {"user": "max@example.com", "has_password": False}}
    shipping.save_login("gls", "max@example.com", "")                      # empty password keeps the old one
    assert shipping.carrier_logins()["gls"]["password"] == "geheim-123"
    with TestClient(app) as c:
        html = c.get("/settings/shipping").text
        assert "max@example.com" in html and "geheim-123" not in html
        c.post("/settings/shipping/login/gls", data={"user": "", "password": ""})
    assert "gls" not in shipping.carrier_logins()                          # cleared user = login removed
    with pytest.raises(shipping.ShippingError):
        shipping.save_login("ups", "x", "y")


LOGIN_FORM = """<html><body><form onsubmit="window.__sent = [this.email.value, this.pw.value]; return false">
 <label>E-Mail <input type=email name=email></label><label>Passwort <input type=password name=pw></label>
 <button type=submit>Anmelden</button></form></body></html>"""


async def test_fill_login_in_real_browser(tmp_path, monkeypatch):
    pw_mod = pytest.importorskip("playwright.async_api")
    monkeypatch.setattr(shipping, "_logins_path", lambda: tmp_path / "carrier-logins.json")
    shipping.save_login("dpd", "max@example.com", "geheim-123")
    monkeypatch.setattr(label_fill, "LOGIN_SETTLE_MS", 100)       # a static page: nothing loads after the login
    async with pw_mod.async_playwright() as pw:
        try:
            browser = await pw.chromium.launch()
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"no chromium: {e}")
        page = await browser.new_page()
        await page.set_content(LOGIN_FORM)
        fields, _ = await label_fill._collect(page)
        assert not any(f["type"] == "password" for f in fields)              # passwords never reach Claude
        assert not await label_fill.fill_login(page, "gls")                  # no login stored for GLS
        assert await label_fill.fill_login(page, "dpd")
        assert await page.evaluate("window.__sent") == ["max@example.com", "geheim-123"]
        await browser.close()


def test_misfit_names_the_broken_rule():
    from app.services.shipping_tariffs import CARRIERS
    pm45 = next(o for o in CARRIERS["post"].options if o.size == "PM 45")
    assert pm45.misfit({"length_cm": 30, "width_cm": 20, "height_cm": 10, "weight_kg": 2}) is None
    assert pm45.misfit({"length_cm": 30, "width_cm": 20, "height_cm": 10, "weight_kg": 12}) == "too heavy: 12 kg > 10 kg"
    assert pm45.misfit({"length_cm": 40, "width_cm": 30, "height_cm": 10, "weight_kg": 2}) == \
        "too big: longest + shortest side 50 cm > 45 cm"
    xl = next(o for o in CARRIERS["dpd"].options if o.size == "XL")
    assert xl.misfit({"length_cm": 180, "width_cm": 10, "height_cm": 10, "weight_kg": 5}) == "too long: 180 cm > 175 cm"
    small = next(o for o in CARRIERS["post"].options if o.size == "Päckchen S")
    assert small.misfit({"length_cm": 30, "width_cm": 20, "height_cm": 5, "weight_kg": 1}) == "too big: 30×20×5 cm > 35.3×25×3 cm"
