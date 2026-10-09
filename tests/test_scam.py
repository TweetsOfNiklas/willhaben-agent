"""Rule-based scam check (app/services/scam.py): synthetic ads only, including cases that must NOT be flagged."""
import copy

import pytest
from sqlmodel import select

from app import db as dbmod
from app import i18n
from app.llm.prompts import screen_prompt
from app.services import scam
from app.services.search_agents import create_agent, run_agent
from tests.conftest import load
from tests.test_search_agents import URL, _set_attr, ha, responses  # noqa: F401  (pytest fixtures)

CTX = {"median": 900.0, "count": 12}       # e.g. a road-bike agent: usual price 900 €


@pytest.fixture(autouse=True)
def english():
    i18n.set_request_lang("en")
    yield


def ad(heading="Rennrad Alu, Rahmen 56", body="Gut erhalten, Abholung in Wien.", price=850.0, private=True):
    return {"heading": heading, "body": body, "price": price, "is_private": private}


def codes(item, ctx=CTX):
    return [f["code"] for f in scam.flags(item, ctx)]


# --- price ------------------------------------------------------------------------------------------------

def test_price_far_below_median_is_high_and_explains_itself():
    fl = scam.flags(ad(price=250), CTX)
    assert fl[0]["code"] == "price_far_below" and fl[0]["severity"] == "high"
    assert fl[0]["label"] == "Price only 28 % of the usual 900 €"
    i18n.set_request_lang("de")
    assert scam.label(fl[0]) == "Preis nur 28 % vom Üblichen (900 €)"


def test_price_below_half_is_warn():
    fl = scam.flags(ad(price=400), CTX)
    assert [(f["code"], f["severity"]) for f in fl] == [("price_below", "warn")]


@pytest.mark.parametrize("item,ctx", [
    (ad(price=600), CTX),                                   # 67 % of the median: just a good deal
    (ad(price=250), {"median": 900.0, "count": 5}),         # too few samples
    (ad(price=250), None),                                  # no context at all
    (ad(price=1), CTX),                                     # 1 € placeholder ("VB")
    (ad(price=0), CTX),                                     # giveaway
    (ad(price=None), CTX),
    (ad(price=40, heading="Rennrad Pedale Shimano"), CTX),  # 4 %: accessory in the search, not a scam
    (ad(price=200, heading="Rennrad defekt, für Bastler"), CTX),
    (ad(price=150, heading="Suche Rennrad Rahmen 56"), CTX),
    (ad(price=250, heading="Rennrad Rahmen + Gabel, Ersatzteile"), CTX),
])
def test_cheap_but_not_suspicious(item, ctx):
    assert codes(item, ctx) == []


# --- text patterns ------------------------------------------------------------------------------------------

def test_western_union_and_vouchers_are_high():
    fl = scam.flags(ad(body="Bezahlung bitte per Western Union, dann schicke ich das Rad."), CTX)
    assert fl[0]["code"] == "money_transfer" and fl[0]["severity"] == "high"
    assert "Western Union" in fl[0]["label"]
    assert "money_transfer" in codes(ad(body="Zahlung gerne mit Paysafecard"))


def test_prepayment_only_without_pickup():
    assert codes(ad(body="Versand nur gegen Vorauskasse.")) == ["prepay_only"]
    assert "prepay_only" in codes(ad(body="Nur Vorkasse, keine Abholung möglich."))


@pytest.mark.parametrize("body", [
    "Abholung in Graz oder Versand nur gegen Vorauskasse.",          # the usual private-seller rule
    "Versand gegen Vorauskasse möglich, gerne auch Barzahlung bei Abholung.",
    "Bezahlung per Überweisung oder bar.",
])
def test_prepayment_as_an_option_is_fine(body):
    assert codes(ad(body=body)) == []


def test_courier_abroad_and_shipping_only_cheap():
    body = "Ich bin derzeit in Spanien, eine Spedition liefert das Rad zu dir."
    assert "courier_abroad" in codes(ad(body=body))
    assert codes(ad(body="Versand per Spedition möglich, Abholung bevorzugt.")) == []   # furniture-style, normal price
    fl = codes(ad(price=300, body="Nur Versand, keine Abholung."))
    assert fl[0] == "price_far_below" and "ship_only_cheap" in fl
    assert "ship_only_cheap" not in codes(ad(price=850, body="Nur Versand, keine Abholung."))


def test_off_platform_contact():
    assert "off_platform" in codes(ad(body="Kontakt nur über WhatsApp bitte."))
    assert "off_platform" in codes(ad(body="Bitte nur per E-Mail melden"))
    assert "email" in codes(ad(body="Schreib an max.muster@example.com"))
    assert "email" in codes(ad(body="max.muster(at)example(punkt)com"))
    # mentioning WhatsApp is common and harmless: info only, never a warning badge
    fl = scam.flags(ad(body="Gerne auch Fragen per WhatsApp."), CTX)
    assert [(f["code"], f["severity"]) for f in fl] == [("messenger", "info")]
    assert scam.warnings(fl) == []
    assert codes(ad(body="Nur Abholung, Fragen gerne per Mail oder Nachricht.")) == []


def test_phone_numbers():
    fl = scam.flags(ad(body="Erreichbar unter 0664 1234567."), CTX)
    assert [(f["code"], f["severity"]) for f in fl] == [("phone", "info")]
    assert codes(ad(body="Tel: +43 664 1234567")) == ["phone"]
    assert codes(ad(body="Ruf an: +49 170 1234567")) == ["phone"]            # neighbour country: normal
    fl = scam.flags(ad(body="Kontakt: +234 803 123 4567"), CTX)
    assert fl[0]["code"] == "foreign_phone" and fl[0]["label"] == "Foreign phone number (+234)"
    assert scam.flags(ad(body="Call +44 7700 900123"), CTX)[0]["params"]["cc"] == "44"


@pytest.mark.parametrize("body", [
    "Gekauft am 01.02.2024, Rechnung vorhanden.",
    "Rahmennummer WTU123456, Baujahr 2021, 22 Gänge, 8,9 kg.",
    "Maße 120 x 60 x 75 cm, Preis 850 € VB.",
    "Laufleistung ca. 1200 km, PLZ 1100 Wien.",
])
def test_numbers_that_are_not_phones(body):
    assert codes(ad(body=body)) == []


def test_iban():
    fl = scam.flags(ad(body="Überweisung auf GB82 WEST 1234 5698 7654 32"), CTX)
    assert fl[0]["code"] == "foreign_iban" and fl[0]["severity"] == "high" and fl[0]["params"] == {"cc": "GB"}
    assert codes(ad(body="Konto DE89 3704 0044 0532 0130 00")) == ["foreign_iban"]
    assert scam.flags(ad(body="Konto DE89 3704 0044 0532 0130 00"), CTX)[0]["severity"] == "warn"
    assert codes(ad(body="Konto AT61 1904 3002 3457 3201")) == ["iban"]
    assert codes(ad(body="Modell GB82 WEST 1234 5698 7654 33")) == []       # wrong checksum: not an IBAN


# --- luxury ------------------------------------------------------------------------------------------------

def test_brand_new_luxury_at_a_fraction():
    ctx = {"median": 1100.0, "count": 20}
    fl = scam.flags(ad(heading="iPhone 16 Pro 256GB neu OVP", body="Originalverpackt, nur Versand.", price=380), ctx)
    assert fl[0]["code"] == "luxury_new_cheap" and fl[0]["severity"] == "high"
    assert "price_far_below" not in [f["code"] for f in fl]          # one price flag is enough
    # without agent statistics only the clear floor cases
    assert codes(ad(heading="Rolex Submariner neu", body="Ungetragen.", price=900), None) == ["luxury_new_cheap"]
    assert codes(ad(heading="Rolex Submariner neu", body="Ungetragen.", price=9000), None) == []


@pytest.mark.parametrize("heading,price", [
    ("iPhone 16 Pro Hülle neu", 15),                # accessory
    ("Rolex Box und Papiere", 300),
    ("Chanel Parfum neu OVP", 60),
    ("PS5 Spiel neu OVP", 30),
    ("iPhone 13 gebraucht", 250),                   # used, not new
])
def test_luxury_false_positives(heading, price):
    assert codes(ad(heading=heading, body="Privatverkauf.", price=price, private=True),
                 {"median": 1000.0, "count": 20}) in ([], ["price_far_below"], ["price_below"])
    assert "luxury_new_cheap" not in codes(ad(heading=heading, body="", price=price), None)


# --- dealer / private ----------------------------------------------------------------------------------------

def test_dealer_private_mismatch_is_info_only():
    fl = scam.flags(ad(body="Rechnung mit MwSt, 12 Monate Gewährleistung.", private=True), CTX)
    assert [(f["code"], f["severity"]) for f in fl] == [("private_dealer_text", "info")]
    assert codes(ad(body="Privatverkauf, keine Garantie.", private=False)) == ["dealer_private_text"]
    assert codes(ad(body="Privatverkauf, keine Gewährleistung und Rücknahme.", private=True)) == []


def test_normal_ads_have_no_flags():
    for item in [
        ad(),
        ad(heading="Kinderfahrrad 20 Zoll", body="Unser Sohn ist rausgewachsen. Abholung in Linz.", price=420,
           private=True),
        ad(heading="E-Bike Trekking 2023", body="Händler mit Service. Probefahrt in unserem Shop.", price=2400,
           private=False),
        ad(heading="Apple Watch Series 9 neu OVP", body="Geschenk, nie getragen. Abholung oder Versand.", price=380),
    ]:
        assert scam.flags(item, {"median": 700.0, "count": 30, "p25": 400.0, "p75": 1100.0}) == [], item


def test_wide_price_spread_disables_the_price_rule():
    # a broad "Fahrrad" search: kids' bikes at 120 €, e-bikes at 3000 € -> the median says nothing
    prices = [90, 120, 150, 180, 250, 400, 700, 900, 1500, 2400, 3000, 3200]
    ctx = scam.median_context(prices)
    assert ctx["count"] == 12 and ctx["p75"] / ctx["p25"] > scam.MAX_SPREAD
    assert codes(ad(heading="Kinderfahrrad 20 Zoll", price=120), ctx) == []
    tight = scam.median_context([800, 820, 850, 880, 900, 900, 920, 950, 990, 1000])
    assert codes(ad(price=250), tight) == ["price_far_below"]
    assert scam.median_context([0, 1, 500])["count"] == 1               # 0/1 € placeholders left out


def test_sorting_headline_and_level():
    fl = scam.flags(ad(price=250, body="Erreichbar unter 0664 1234567, nur per WhatsApp."), CTX)
    assert [f["severity"] for f in fl] == ["high", "warn", "info"]
    assert scam.level(fl) == "high"
    assert scam.headline(fl) == "Price only 28 % of the usual 900 €"
    assert scam.level([]) is None and scam.headline(None) == ""


def test_works_for_detail_objects():
    from app.willhaben.parse import AdDetail
    d = AdDetail(ad_id="1000000001", heading="Rennrad", description="Bezahlung nur per MoneyGram", price=500,
                 price_display="€ 500", status="active", published_at=None, first_published_at=None, end_date=None,
                 url="https://www.willhaben.at/iad/object?adId=1000000001")
    assert [f["code"] for f in scam.for_viewer(d, None)] == ["money_transfer"]
    assert scam.for_viewer(None, None) == []


def test_screening_prompt_gets_the_flags():
    p = screen_prompt(agent_name="Rennrad", brief="", stats=None, language="Deutsch", hits=[
        {"nr": 1, "title": "Rennrad", "price": "€ 250", "location": "Wien", "private": True, "description": "x",
         "photo": None, "scam": ["Preis nur 28 % vom Üblichen (900 €)"]}])
    assert "Betrugs-Warnhinweise (Regelprüfung): Preis nur 28 % vom Üblichen (900 €)" in p


# --- integration: run_agent stores flags and warns in the push ------------------------------------------------

def _page(n_normal: int, extra: list[tuple[str, str, str, float]]):
    base = load("search_fahrrad_rows3.json")
    tmpl = base["advertSummaryList"]["advertSummary"][0]
    ads = []
    rows = [(f"10000000{i:02d}", f"Rennrad Modell {i}", "Gepflegt, Abholung in Wien.", 800.0 + i * 20)
            for i in range(1, n_normal + 1)] + extra
    for ad_id, heading, body, price in rows:
        a = copy.deepcopy(tmpl)
        a["id"] = ad_id
        for k, v in [("ADID", ad_id), ("HEADING", heading), ("BODY_DYN", body), ("PRICE", f"{price:g}"),
                     ("PRICE/AMOUNT", f"{price}"), ("PRICE_FOR_DISPLAY", f"€ {price:g}"), ("LOCATION", "Wien"),
                     ("ISPRIVATE", "1")]:
            _set_attr(a, k, v)
        ads.append(a)
    base["advertSummaryList"]["advertSummary"] = ads
    base["rowsFound"] = len(ads)
    return base


async def test_run_agent_flags_new_hits_and_warns(responses, ha):  # noqa: F811
    queue, _ = responses
    agent = create_agent(URL)
    queue.append(_page(10, []))
    await run_agent(agent.id)                         # baseline: 10 normal road bikes, nothing pushed
    with dbmod.session() as db:
        assert all(h.scam_flags is None for h in db.exec(select(dbmod.Hit)))

    scam_ad = ("1000000101", "Rennrad Carbon wie neu", "Ich bin im Ausland, Kurier bringt es. Zahlung per Western Union.", 290.0)
    fine_ad = ("1000000102", "Rennrad Stahl Klassiker", "Abholung in Wien-Favoriten.", 650.0)
    queue.append(_page(10, [scam_ad, fine_ad]))
    r = await run_agent(agent.id)
    assert {h.ad_id for h in r.new} == {"1000000101", "1000000102"}
    with dbmod.session() as db:
        flagged = db.exec(select(dbmod.Hit).where(dbmod.Hit.ad_id == "1000000101")).one()
        fine = db.exec(select(dbmod.Hit).where(dbmod.Hit.ad_id == "1000000102")).one()
    got = {f["code"] for f in flagged.scam_flags}
    assert {"price_far_below", "money_transfer", "courier_abroad"} <= got
    assert fine.scam_flags is None

    events = {b["ad_id"]: b for p, b in ha if p.endswith("willhaben_hit")}
    assert events["1000000101"]["title"].startswith("⚠️ ")
    assert "\n⚠️ " in events["1000000101"]["message"]
    assert events["1000000101"]["scam_level"] == "high" and "money_transfer" in events["1000000101"]["scam_flags"]
    assert not events["1000000102"]["title"].startswith("⚠️") and "scam_level" not in events["1000000102"]


async def test_flags_are_set_before_screening(responses, ha, monkeypatch):  # noqa: F811
    from app.services import screening
    queue, _ = responses
    agent = create_agent(URL)
    with dbmod.session() as db:
        a = db.get(dbmod.SearchAgent, agent.id)
        a.screen = True
        db.add(a)
        db.commit()
    queue.append(_page(10, []))
    await run_agent(agent.id)
    seen = {}

    async def fake_screen(agent_id, hits):
        seen.update({h.ad_id: h.scam_flags for h in hits})
        return 0
    monkeypatch.setattr(screening, "screen", fake_screen)
    queue.append(_page(10, [("1000000201", "Rennrad", "Nur Vorkasse, keine Abholung.", 820.0)]))
    await run_agent(agent.id)
    assert [f["code"] for f in seen["1000000201"]] == ["prepay_only"]


def test_hit_card_and_viewer_render_badges():
    from fastapi.testclient import TestClient

    from app.main import app
    with dbmod.session() as db:
        a = dbmod.SearchAgent(name="Rennrad", source_url=URL, webapi_url=URL)
        db.add(a)
        db.commit()
        db.refresh(a)
        db.add(dbmod.Hit(agent_id=a.id, ad_id="1000000301", heading="Rennrad Carbon", url="https://www.willhaben.at/x",
                         price=250, scam_flags=scam.flags(ad(price=250, body="Nur per WhatsApp, Tel 0664 1234567"), CTX)))
        db.commit()
    with TestClient(app) as c:
        html = c.get("/hits", headers={"accept-language": "de"}).text
    assert "Preis nur 28 % vom Üblichen (900 €)" in html and "Will Kontakt außerhalb von willhaben" in html
    assert "Telefonnummer im Text" not in html          # info flags stay out of the hit cards


# --- review fixes: wanted/swap ads, body words, unscreened hits, rejected hits ------------------------------------

@pytest.mark.parametrize("heading,price", [("Suche iPhone 16 Pro neu", 250), ("Tausche PS5 neu gegen Switch", 100)])
def test_wanted_and_swap_ads_are_not_luxury_scams(heading, price):
    assert codes(ad(heading=heading, body="Bitte melden.", price=price), None) == []
    assert codes(ad(heading=heading, body="Bitte melden.", price=price), {"median": 1000.0, "count": 20}) == []


@pytest.mark.parametrize("body", ["Neu im Originalkarton, Versand gratis.", "Inkl. Hülle und Case.",
                                  "Ich suche einen schnellen Käufer.", "Tausch ausgeschlossen."])
def test_common_body_words_do_not_switch_off_the_price_rule(body):
    assert codes(ad(price=250, body=body)) == ["price_far_below"]


def test_viewer_counts_unscreened_hits():
    from app.willhaben.parse import AdDetail
    with dbmod.session() as db:
        a = dbmod.SearchAgent(name="Rennrad", source_url=URL, webapi_url=URL)
        db.add(a)
        db.commit()
        db.refresh(a)
        for i in range(10):                                  # never screened: screen_relevant stays NULL
            db.add(dbmod.Hit(agent_id=a.id, ad_id=f"10000004{i:02d}", heading=f"Rennrad {i}", url="https://x",
                             price=850 + i * 10))
        for i in range(10):                                  # rejected by screening: left out of the median
            db.add(dbmod.Hit(agent_id=a.id, ad_id=f"10000005{i:02d}", heading=f"Pedal {i}", url="https://x",
                             price=20, screen_relevant=False))
        h = dbmod.Hit(agent_id=a.id, ad_id="1000000499", heading="Rennrad Carbon", url="https://x", price=250)
        db.add(h)
        db.commit()
        db.refresh(h)
    ctx = scam.agent_context(h.agent_id, {h.id})
    assert ctx["count"] == 10 and ctx["median"] == 895
    d = AdDetail(ad_id="1000000499", heading="Rennrad Carbon", description="Abholung in Wien.", price=250,
                 price_display="€ 250", status="active", published_at=None, first_published_at=None, end_date=None,
                 url="https://www.willhaben.at/iad/object?adId=1000000499")
    assert [f["code"] for f in scam.for_viewer(d, h)] == ["price_far_below"]


def test_scam_context_leaves_out_rejected_page_ads():
    from types import SimpleNamespace as NS

    from app.services.search_agents import _scam_context
    hits = [NS(ad_id=f"10000006{i:02d}", price=900.0, screen_relevant=None, first_seen_at=None) for i in range(8)]
    hits += [NS(ad_id=f"10000007{i:02d}", price=20.0, screen_relevant=False, first_seen_at=None) for i in range(8)]
    ads = [NS(ad_id=f"10000007{i:02d}", price=20.0) for i in range(8)] + [NS(ad_id="1000000800", price=950.0)]
    ctx = _scam_context(hits, ads)
    assert ctx["count"] == 9 and ctx["median"] == 900.0
