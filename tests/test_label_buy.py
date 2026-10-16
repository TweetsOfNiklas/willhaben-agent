import http.server
import json
import re
import threading
import stat
from urllib.parse import parse_qs, urlsplit

import pytest
from types import SimpleNamespace
from fastapi.testclient import TestClient

from app.db import Conversation, MyAd, Shipment, session
from app.llm.base import LLMError, LLMResult
from app.main import app
from app.services import label_buy, label_fill, shipping
from app.services.shipping_tariffs import CARRIERS  # noqa: F401 - module is imported for its side effects

ADDR = {"name": "Max Muster", "street": "Hauptstraße", "house_no": "12", "extra": "", "zip": "1150",
        "city": "Wien", "phone": "", "email": ""}
PDF = b"%PDF-1.4 fake label"
CARD = ("Max Muster", "4111 1111 1111 1111", "12/30", "123")


# ------------------------------------------------------------------ pure guards
def test_page_total_prefers_the_total_line():
    assert label_buy.page_total("Paketgebühr 3,20 €\nGesamt 4,90 €") == 4.9
    assert label_buy.page_total("Gesamt 1.234,50 €") == 1234.5
    assert label_buy.page_total("Ab 3,20 € bis 85,00 € je Paket") == 85.0       # no total word: the largest amount
    assert label_buy.page_total("kein Preis hier") is None


def test_refuse_and_purchase_words():
    for text in ("Konto löschen", "Newsletter abonnieren", "Abmelden", "Storno", "Jetzt registrieren"):
        assert label_buy._REFUSE.search(text), text
    for text in ("Kostenpflichtig kaufen", "Jetzt zahlen", "Zahlungspflichtig bestellen"):
        assert label_buy._PURCHASE.search(text), text
    assert not label_buy._PURCHASE.search("PayPal anmelden")                    # "pay" only as a whole word
    for text in ("Weiter einkaufen", "Zur Bestellübersicht", "Zahlungsart ändern"):   # navigation, no price check
        assert not label_buy._PURCHASE.search(text), text
    assert not label_buy._REFUSE.search("Weiter")


def test_label_downloads_are_told_from_other_pdfs():
    assert label_buy.label_download("Paketschein_123.pdf") and label_buy.label_download("label.pdf")
    for name in ("AGB.pdf", "Rechnung-2026.pdf", "Datenschutz.pdf", "info.txt"):
        assert not label_buy.label_download(name), name


async def test_other_downloads_before_the_purchase_are_not_the_label(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(data_dir=tmp_path))
    monkeypatch.setattr(label_buy, "get_settings", lambda: SimpleNamespace(data_dir=tmp_path))
    sid = _seed()
    saved = []

    class Download:
        suggested_filename = "AGB.pdf"

        async def save_as(self, path):
            saved.append(path)

    label_buy._active.update(shipment=sid, bought=False)
    await label_buy._download(sid, Download())
    assert saved and saved[0].name == "doc-AGB.pdf" and shipping.get(sid).label_path is None
    label_buy._active.update(shipment=None, bought=False)


def test_bank_hosts_and_texts():
    assert label_buy._BANK_HOST.search("acs.bank.example") and label_buy._BANK_HOST.search("secure.psp.example")
    assert not label_buy._BANK_HOST.search("www.gls-one.at")
    assert label_buy._BANK_TEXT.search("Bitte bestätigen Sie die Zahlung")
    assert label_buy._BANK_TEXT.search("Geben Sie die TAN ein")
    assert not label_buy._BANK_TEXT.search("Standard-Versand, Tankstelle")      # TAN only as a word, case-sensitive


# ------------------------------------------------------------------ payment card
@pytest.fixture
def card_file(monkeypatch, tmp_path):
    p = tmp_path / "payment-card.json"
    monkeypatch.setattr(label_buy, "_card_path", lambda: p)
    return p


def test_card_is_stored_private_and_never_shown_whole(card_file):
    assert label_buy.card_status() == {"stored": False, "last4": "", "holder": "", "expiry": ""}
    label_buy.save_card(*CARD)
    assert stat.S_IMODE(card_file.stat().st_mode) == 0o600
    status = label_buy.card_status()
    assert status == {"stored": True, "last4": "1111", "holder": "Max Muster", "expiry": "12/30"}
    assert "4111" not in json.dumps(status)
    assert label_buy.card()["number"] == "4111111111111111"
    label_buy.remove_card()
    assert label_buy.card_status()["stored"] is False and not card_file.exists()


@pytest.mark.parametrize("args, msg", [
    (("Max Muster", "4111 1111 1111 1112", "12/30", ""), "not valid"),          # Luhn
    (("Max Muster", "4111 1111 1111 1111", "01/20", ""), "expired"),
    (("Max Muster", "4111 1111 1111 1111", "13/30", ""), "01 to 12"),
    (("Max Muster", "4111 1111 1111 1111", "1230", ""), "MM/YY"),
    (("Max Muster", "4111 1111 1111 1111", "12/30", "12"), "CVC"),
    (("", "4111 1111 1111 1111", "12/30", ""), "name"),
])
def test_invalid_card_is_refused(card_file, args, msg):
    with pytest.raises(label_buy.CardError, match=msg):
        label_buy.save_card(*args)
    assert not card_file.exists()


def test_card_settings_routes(card_file):
    with TestClient(app) as c:
        r = c.post("/settings/shipping/card", data={"holder": CARD[0], "number": CARD[1], "expiry": CARD[2], "cvc": CARD[3]},
                   follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/settings/shipping?msg=")
        html = c.get("/settings/shipping").text
        assert "•••• 1111" in html and "4111 1111 1111 1111" not in html and "4111111111111111" not in html
        c.post("/settings/shipping/card/delete", follow_redirects=False)
        assert not label_buy.card_status()["stored"]
        r = c.post("/session/label-buy/step", data={"next": "/session"}, follow_redirects=False)
        assert r.status_code == 303 and "msg=" in r.headers["location"]          # nothing running: a message, no crash


async def test_step_without_checkout_says_so():
    with pytest.raises(label_buy.BuyError, match="No checkout open"):
        await label_buy.step_by_hand()
    assert label_buy.active() is None


# ------------------------------------------------------------------ fake carrier site + fake Claude
STEP1 = """<html><body><form>
 <h3>Empfänger</h3>
 <label>Name <input name="rcpt_name" autocomplete="name"></label>
 <label>PLZ <input name="rcpt_zip"></label>
 <label><input type="radio" name="size" value="M"> Größe M</label>
 <label><input type="checkbox" name="agb"> Ich akzeptiere die AGB</label>
 <label><input type="checkbox" name="versicherung"> Zusatzversicherung</label>
</form>
<button type="button" onclick="fetch('/record/step1',{method:'POST',body:new URLSearchParams(new FormData(document.forms[0]))}).then(()=>location.href='/step2')">Weiter</button>
<a href="#" onclick="fetch('/clicked/konto');return false">Konto löschen</a>
</body></html>"""

def step3(pdf_url: str) -> str:
    return f"""<html><body><h3>Ihr Paketschein ist bereit</h3>
<a href="{pdf_url}" download>Paketschein herunterladen</a><a href="/impressum">Impressum</a></body></html>"""


def step2(total: str, target: str = "/step3") -> str:
    return f"""<html><body><form>
 <label><input type="radio" name="pay" value="cc"> Kreditkarte</label>
 <input name="holder" autocomplete="cc-name" placeholder="Name auf der Karte">
 <input name="card" autocomplete="cc-number" placeholder="Kartennummer">
 <input name="exp" autocomplete="cc-exp" placeholder="MM/YY">
 <input name="csc" autocomplete="cc-csc" placeholder="CVC">
</form>
<p>Gesamt {total} €</p>
<button type="button" onclick="fetch('/record/step2',{{method:'POST',body:new URLSearchParams(new FormData(document.forms[0]))}}).then(()=>location.href='{target}')">Kostenpflichtig kaufen</button>
</body></html>"""


def _items(prompt: str):
    fields = json.loads(re.search(r"^Formularfelder: (.*)$", prompt, re.M).group(1))
    actions = json.loads(re.search(r"^Schaltflächen und Links: (.*)$", prompt, re.M).group(1))
    return fields, actions


def _field(prompt: str, name: str) -> int:
    return next(f["idx"] for f in _items(prompt)[0] if f.get("name") == name)


def _action(prompt: str, text: str) -> int:
    return next(a["idx"] for a in _items(prompt)[1] if a["label"] == text)


class FakeClaude:
    """Answers per carrier path; the last scripted answer repeats. An exception in the script is raised."""
    model = "fake"

    def __init__(self, script):
        self.script = {k: list(v) for k, v in script.items()}
        self.prompts: list[str] = []

    async def complete(self, *, system, prompt, schema, images=None):
        self.prompts.append(prompt)
        path = urlsplit(re.search(r"^Seite: (\S+)$", prompt, re.M).group(1)).path
        queue = self.script[path]
        step = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(step, Exception):
            raise step
        return LLMResult(data=step(prompt) if callable(step) else step, model="fake")


class _PdfHandler(http.server.BaseHTTPRequestHandler):
    """Chromium cancels downloads of responses that a route fulfilled, so the label comes from a local server."""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Disposition", "attachment; filename=paketschein.pdf")
        self.send_header("Content-Length", str(len(PDF)))
        self.end_headers()
        self.wfile.write(PDF)

    def log_message(self, *args):
        pass


class FakeSession:
    started = True

    def __init__(self, context):
        self.context = context


class Harness:
    def __init__(self, context):
        self.context = context
        self.site: dict[str, str] = {}
        self.rec: list[tuple[str, str]] = []
        self.pdf_url = ""

    def paths(self):
        return [p for p, _ in self.rec]


@pytest.fixture
async def checkout(monkeypatch, tmp_path):
    pw_mod = pytest.importorskip("playwright.async_api")
    async with pw_mod.async_playwright() as pw:
        browser = None
        for kwargs in ({}, {"channel": "chrome"}):              # bundled Chromium, else the installed Google Chrome
            try:
                browser = await pw.chromium.launch(**kwargs)
                break
            except Exception:  # noqa: BLE001 - not installed under this name
                continue
        if browser is None:
            pytest.skip("no Chromium or Google Chrome for Playwright")
        ctx = await browser.new_context(accept_downloads=True)
        h = Harness(ctx)
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _PdfHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        h.pdf_url = f"http://127.0.0.1:{srv.server_address[1]}/label.pdf"
        await _serve(ctx, h)
        monkeypatch.setattr("app.willhaben.session.get_session", lambda: FakeSession(ctx))
        monkeypatch.setattr(label_buy, "_card_path", lambda: tmp_path / "payment-card.json")
        monkeypatch.setattr(label_buy, "LABEL_WAIT_S", 1.5)     # the fake site's last step clicks no download
        monkeypatch.setattr(label_buy, "LABEL_POLL_S", 0.1)
        monkeypatch.setattr(label_buy, "SETTLE_MS", 200)        # the fake site is local and static
        monkeypatch.setattr(label_buy, "CARD_SETTLE_MS", 200)
        monkeypatch.setattr(label_buy, "_playbook_path", lambda carrier: tmp_path / "playbooks" / f"{carrier}.json")
        label_buy._active.update(shipment=None, page=None, step=0, bought=False, label_before=None, pos=0, learn=[],
                                 playbook=None)
        yield h
        await browser.close()
        srv.shutdown()


async def _serve(ctx, h: Harness) -> None:
    async def handle(route):
        path = urlsplit(route.request.url).path
        if path.startswith(("/record", "/clicked")):
            h.rec.append((path, route.request.post_data or ""))
            await route.fulfill(status=204, body="")
        elif path in h.site:
            await route.fulfill(status=200, body=h.site[path], headers={"content-type": "text/html; charset=utf-8"})
        else:
            await route.fulfill(status=404, body="")

    await ctx.route("https://www.gls-one.at/**", handle)


def _seed(option: str = "gls:S:home", approval: str | None = None) -> int:
    o = shipping.by_key(option)
    assert o and o.online and CARRIERS["gls"].label_url.startswith("https://www.gls-one.at/")
    with session() as db:
        db.add(MyAd(ad_id="77", heading="Kinderbuch"))
        db.add(Conversation(id="c1", ad_id="77", ad_title="Kinderbuch", partner_name="Max", role="seller",
                            last_message_text=""))
        db.commit()
        s = Shipment(conversation_id="c1", ad_id="77", status="buying", recipient=ADDR, approval=approval,
                     parcel={"length_cm": 30, "width_cm": 20, "height_cm": 5, "weight_kg": 1.0},
                     option=o.key, price=o.price)
        db.add(s)
        db.commit()
        db.refresh(s)
        return s.id


def _good_site(h: Harness, total: str) -> None:
    h.site.update({"/de/parcel/": STEP1, "/step2": step2(total), "/step3": step3(h.pdf_url)})


def _script(*, step2_answer=None, step3=None):
    step1_first = lambda p: {"fills": [  # noqa: E731
        {"idx": _field(p, "rcpt_name"), "value": "Max Muster"}, {"idx": _field(p, "rcpt_zip"), "value": "1150"},
        {"idx": _field(p, "size"), "value": "check"}, {"idx": _field(p, "agb"), "value": "check"},
        {"idx": _field(p, "versicherung"), "value": "check"}],                       # must be refused
        "click": _action(p, "Konto löschen"), "status": "continue", "note": "Adresse eintragen"}  # must be refused
    step1_next = lambda p: {"fills": [], "click": _action(p, "Weiter"), "status": "continue", "note": "weiter"}  # noqa: E731
    buy_now = step2_answer or (lambda p: {"fills": [], "click": _action(p, "Kostenpflichtig kaufen"),
                                          "status": "continue", "note": "kaufen"})
    return {"/de/parcel/": [step1_first, step1_next], "/step2": [buy_now],
            "/step3": [step3 or {"fills": [], "click": 0, "status": "done", "note": "fertig"}],
            "/bank": [{"fills": [], "click": 0, "status": "continue", "note": "x"}]}


@pytest.fixture
def claude(monkeypatch):
    holder = {}

    def use(script):
        fake = FakeClaude(script)
        fake.purposes = []                          # the get_backend(purpose) of each call

        def backend(purpose=None):
            fake.purposes.append(purpose)
            return fake
        monkeypatch.setattr("app.llm.base.get_backend", backend)
        holder["fake"] = fake
        return fake
    return use


# ------------------------------------------------------------------ browser: the whole checkout
async def test_buy_fills_pays_and_saves_the_label(checkout, claude, monkeypatch):
    label_buy.save_card(*CARD)
    _good_site(checkout, f"{shipping.by_key('gls:S:home').price:.2f}".replace(".", ","))
    fake = claude(_script())
    sid = _seed()

    s = await label_buy.buy(sid)

    assert s.status == "labelled" and s.error is None and s.paid_price == shipping.by_key("gls:S:home").price
    assert fake.purposes and set(fake.purposes) == {"shipping"}       # Settings → Claude → "Shipping & labels"
    assert (label_buy.get_settings().data_dir / "labels" / s.label_path).read_bytes() == PDF
    steps = [e["step"] for e in s.buy_log]
    assert steps == sorted(steps) and len(steps) >= 3
    assert all("?" not in e["url"] for e in s.buy_log)
    assert any(e["action"] == "Kostenpflichtig kaufen" for e in s.buy_log)
    recs = dict(checkout.rec)
    step1 = parse_qs(recs["/record/step1"])
    assert step1["rcpt_name"] == ["Max Muster"] and step1["rcpt_zip"] == ["1150"]
    assert step1["size"] == ["M"] and step1["agb"] == ["on"] and "versicherung" not in step1  # insurance refused
    step2 = parse_qs(recs["/record/step2"])
    assert step2["card"] == ["4111111111111111"] and step2["exp"] == ["12/30"] and step2["csc"] == ["123"]
    assert step2["holder"] == ["Max Muster"]
    assert "/clicked/konto" not in checkout.paths()                              # Konto löschen never clicked
    assert all("4111" not in p and "4111111111111111" not in p for p in fake.prompts)  # card never reaches Claude
    assert (label_buy.get_settings().data_dir / "labels" / f"shipment-{sid}" / "step-1.png").exists()
    assert label_buy.active() is None


async def test_auto_limit_only_applies_to_automatic_purchases(checkout, claude):
    """A manually approved label above the automatic limit is still bought (the limit is an auto rule)."""
    from app.db import set_setting
    set_setting("ship_auto_max_price", "4")
    _good_site(checkout, "4,90")
    label_buy.save_card(*CARD)
    claude(_script())
    sid = _seed()
    shipping.update(sid, approval="approved")
    await label_buy.buy(sid)
    assert shipping.get(sid).status == "labelled" and shipping.get(sid).paid_price == 4.9
    shipping.update(sid, approval="auto", status="buying", label_path=None)
    claude(_script())
    with pytest.raises(label_buy.BuyError, match="limit"):
        await label_buy.buy(sid)


async def test_price_cap_is_the_shared_default_of_ten_euro():
    """`_cap` reads the same setting as `shipping.auto_blockers`: 10 € when nothing was ever saved."""
    from app.db import set_setting

    class Page:
        def __init__(self, text):
            self.text = text

        async def inner_text(self, selector):
            return self.text

    assert label_buy._cap() == 10.0 == shipping.auto_config()["max_price"]
    sid = _seed(approval="auto")
    shipping.update(sid, price=12.0)
    s = shipping.get(sid)
    with pytest.raises(label_buy.BuyError, match="limit €10,00"):
        await label_buy._check_price(Page("Gesamt 12,00 €"), s)
    shipping.update(sid, approval="approved")
    assert await label_buy._check_price(Page("Gesamt 12,00 €"), shipping.get(sid)) == 12.0   # by hand: no cap
    set_setting("ship_auto_max_price", "7.5")
    assert label_buy._cap() == 7.5
    set_setting("ship_auto_max_price", "0")
    assert label_buy._cap() == 0.0                                                  # 0 = nothing automatic
    with pytest.raises(label_buy.BuyError, match="limit €0,00"):
        await label_buy._check_price(Page("Gesamt 4,90 €"), shipping.update(sid, approval="auto", price=4.9))


async def test_shop_option_needs_a_parcel_shop_before_the_checkout(monkeypatch):
    """GLS S → Shop without a shop near the buyer: refused before any page opens; with one, the checkout data
    names it as empfaenger.paketshop (the playbooks key it like every other field)."""
    lookups = []

    async def no_shops(s):
        lookups.append(s.option)
        return []

    monkeypatch.setattr(shipping, "shops_near", no_shops)
    sid = _seed("gls:S:shop")
    with pytest.raises(label_buy.BuyError, match="no parcel shop found near the buyer"):
        await label_buy.buy(sid)
    assert lookups == ["gls:S:shop"] and shipping.get(sid).status == "failed"
    assert label_buy.active() is None
    shipping.set_shop(sid, {"name": "Trafik", "address": "C 3, 1150 Wien", "id": "AT-1001"})
    flat = label_buy._flat(label_fill._data_for(shipping.get(sid)))
    assert flat["empfaenger.paketshop.name"] == "Trafik" and flat["empfaenger.paketshop.id"] == "AT-1001"
    assert "empfaenger.shop" not in " ".join(flat) and flat["paket.zustellung"] == "shop"


async def test_retry_resumes_the_open_checkout(monkeypatch):
    """After a NeedsHuman stop the page stays open; buy() for the same shipment carries on instead of failing."""
    sid = _seed()
    page = SimpleNamespace(is_closed=lambda: False)
    label_buy._active.update(shipment=sid, page=page)
    driven = []

    async def drive(s, p, steps):
        driven.append((s, p, steps))
        return "labelled"

    monkeypatch.setattr(label_buy, "_drive", drive)
    await label_buy.buy(sid)
    assert driven == [(sid, page, label_buy.MAX_STEPS)]
    with pytest.raises(label_buy.BuyError, match="still open"):                # another shipment must wait
        await label_buy.buy(sid + 1000)
    label_buy._active.update(shipment=None, page=None)


async def test_price_guard_stops_before_paying(checkout, claude):
    _good_site(checkout, "9,90")
    label_buy.save_card(*CARD)
    claude(_script())
    sid = _seed()

    with pytest.raises(label_buy.BuyError, match="price on the page"):
        await label_buy.buy(sid)

    s = shipping.get(sid)
    assert s.status == "failed" and "price on the page" in s.error and s.paid_price is None
    assert "/record/step2" not in checkout.paths()                              # the purchase button was not pressed
    assert label_buy.active() is None


async def test_3ds_pauses_for_the_user(checkout, claude):
    label_buy.save_card(*CARD)
    checkout.site.update({"/de/parcel/": STEP1, "/step2": step2("4,90", "/bank"),
                          "/bank": "<html><body><p>Bitte bestätigen Sie die Zahlung in Ihrer Bank-App.</p></body></html>"})
    claude(_script())
    sid = _seed()

    with pytest.raises(label_buy.NeedsHuman, match="bank app"):
        await label_buy.buy(sid)

    s = shipping.get(sid)
    assert s.status == "buying" and "bank app" in s.error
    assert label_buy.active()["shipment_id"] == sid                              # the page stays open for the user
    await label_buy._close(label_buy._active["page"])


async def test_missing_card_pauses_for_the_user(checkout, claude):
    _good_site(checkout, "4,90")
    claude(_script())
    sid = _seed()

    with pytest.raises(label_buy.NeedsHuman, match="no payment card stored"):
        await label_buy.buy(sid)
    assert shipping.get(sid).status == "buying"
    assert "/record/step2" not in checkout.paths()
    await label_buy._close(label_buy._active["page"])


async def test_claude_failure_is_a_buy_error(checkout, claude):
    _good_site(checkout, "4,90")
    claude({"/de/parcel/": [LLMError("claude down")]})
    sid = _seed()

    with pytest.raises(label_buy.BuyError, match="could not read"):
        await label_buy.buy(sid)
    s = shipping.get(sid)
    assert s.status == "failed" and "could not read" in s.error and label_buy.active() is None


async def test_step_limit_is_a_buy_error(checkout, claude, monkeypatch):
    monkeypatch.setattr(label_buy, "MAX_STEPS", 2)
    _good_site(checkout, "4,90")
    claude({"/de/parcel/": [{"fills": [], "click": 0, "status": "continue", "note": "warte"}]})
    sid = _seed()

    with pytest.raises(label_buy.BuyError, match="no label after 2 steps"):
        await label_buy.buy(sid)
    assert shipping.get(sid).status == "failed"


def _rerun(sid: int) -> None:
    """Puts the shipment back to "buying" as the pipeline would for a new attempt."""
    shipping.update(sid, status="buying", label_path=None, error=None, buy_log=None)


async def test_playbook_is_learned_then_replayed_without_claude(checkout, claude):
    label_buy.save_card(*CARD)
    _good_site(checkout, f"{shipping.by_key('gls:S:home').price:.2f}".replace(".", ","))
    claude(_script())
    sid = _seed()
    first = await label_buy.buy(sid)
    assert first.status == "labelled" and [e["mode"] for e in first.buy_log] == ["claude"] * 4

    pb = label_buy.load_playbook("gls")
    assert [st["url_pattern"] for st in pb["steps"]] == ["www.gls-one.at/de/parcel/", "www.gls-one.at/de/parcel/",
                                                         "www.gls-one.at/step2", "www.gls-one.at/step3"]
    assert {"selector": {"kind": "name", "tag": "input", "value": "rcpt_zip"}, "field": "empfaenger.zip"} in pb["steps"][0]["fills"]
    assert pb["steps"][3]["done"] is True
    stored = json.dumps(pb)
    assert "1150" not in stored and "Max Muster" not in stored and "4111" not in stored   # keys, never values
    assert label_buy.playbook_status()["gls"] | {"learned_at": "", "last_success_at": ""} == {
        "name": "GLS", "learned": True, "learned_at": "", "last_success_at": "", "steps": 4}

    checkout.rec.clear()
    _rerun(sid)
    fake = claude({})                                       # any call to Claude would fail the run
    second = await label_buy.buy(sid)
    assert second.status == "labelled"
    assert fake.prompts == []                               # replayed without Claude
    assert [e["mode"] for e in second.buy_log] == ["playbook"] * 4
    recs = dict(checkout.rec)
    assert parse_qs(recs["/record/step1"])["rcpt_zip"] == ["1150"] and parse_qs(recs["/record/step1"])["size"] == ["M"]
    assert "/clicked/konto" not in checkout.paths()
    assert label_buy.load_playbook("gls")["version"] == pb["version"]          # unchanged run: same version


async def test_changed_page_asks_claude_for_that_step_only(checkout, claude):
    label_buy.save_card(*CARD)
    _good_site(checkout, "4,90")
    claude(_script())
    sid = _seed()
    await label_buy.buy(sid)
    before = label_buy.load_playbook("gls")

    checkout.site["/de/parcel/"] = STEP1.replace(">Weiter<", ">Weiter zur Zahlung<")
    _rerun(sid)
    fake = claude({"/de/parcel/": [lambda p: {"fills": [], "click": _action(p, "Weiter zur Zahlung"),
                                              "status": "continue", "note": "neuer Knopf"}]})
    s = await label_buy.buy(sid)

    assert s.status == "labelled"
    assert len(fake.prompts) == 1                           # only the changed step went to Claude
    assert [e["mode"] for e in s.buy_log] == ["playbook", "claude", "playbook", "playbook"]
    after = label_buy.load_playbook("gls")
    assert after["steps"][1]["action"]["text"] == "Weiter zur Zahlung"
    assert after["version"] == before["version"] + 1


async def test_replayed_checkout_keeps_the_price_guard(checkout, claude):
    label_buy.save_card(*CARD)
    _good_site(checkout, "4,90")
    claude(_script())
    sid = _seed()
    await label_buy.buy(sid)

    checkout.site["/step2"] = step2("9,90")
    checkout.rec.clear()
    _rerun(sid)
    fake = claude({})
    with pytest.raises(label_buy.BuyError, match="price on the page"):
        await label_buy.buy(sid)
    assert fake.prompts == [] and "/record/step2" not in checkout.paths()
    assert shipping.get(sid).status == "failed"


def test_reset_playbook_route_removes_it(tmp_path, monkeypatch):
    monkeypatch.setattr(label_buy, "_playbook_path", lambda carrier: tmp_path / f"{carrier}.json")
    label_buy._save_playbook("gls", [{"url_pattern": "x", "fills": [], "action": None, "done": True, "replay": True}])
    assert label_buy.load_playbook("gls")["steps"]
    with TestClient(app) as c:
        assert "Reset playbook" in c.get("/settings/shipping").text
        r = c.post("/settings/shipping/playbook/gls/reset", follow_redirects=False)
        assert r.status_code == 303 and "msg=" in r.headers["location"]
        assert c.post("/settings/shipping/playbook/nope/reset", follow_redirects=False).status_code == 303
    assert label_buy.load_playbook("gls") is None
    assert label_buy.playbook_status()["gls"]["learned"] is False
