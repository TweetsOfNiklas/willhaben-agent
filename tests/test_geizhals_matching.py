"""Geizhals matching on real search results (names, links and prices from geizhals.at, 2026-10-13): the cases where
the first result was another variant, an accessory or a pack, or the right product was turned down."""
import httpx
import pytest

from app.services import deals, geizhals
from app.services.geizhals import Hit

GH = "https://geizhals.at/"


def _hits(rows):
    return [Hit(name=n, url=u if u.startswith("http") else GH + u, price=p) for n, u, p in rows]


IPHONE_16_PRO = _hits([
    ("Apple iPhone 16 128GB schwarz", "apple-iphone-16-128gb-schwarz-a3296281.html", 842.0),
    ("Apple iPhone 16e 128GB schwarz", "apple-iphone-16e-128gb-schwarz-a3383000.html", 640.0),
    ("Apple iPhone 16 Pro Max 512GB Titan Schwarz", "apple-iphone-16-pro-max-512gb-titan-schwarz-a3296274.html", 1649.0),
    ("Apple iPhone 16 Pro 256GB Titan Weiß", "apple-iphone-16-pro-256gb-titan-weiss-a3296260.html", 1789.99),
])
IPHONE_14_PRO = _hits([
    ("Apple iPhone 14 Pro Max 128GB Space Schwarz", "apple-iphone-14-pro-max-128gb-space-schwarz-a2806656.html", 1081.0),
    ("Apple iPhone 14 Pro 128GB dunkellila", "apple-iphone-14-pro-128gb-dunkellila-a2807069.html", 854.0),
])
PI_5 = _hits([
    ("Raspberry Pi 5 Modell B, 4GB RAM, verschiedene Bundles", "raspberry-pi-5-modell-b-a3081796.html", 137.9),
    ("Raspberry Pi Compute Module 5 4GB", "raspberry-pi-compute-module-5-4gb-sc1562-a3405623.html", 107.8),
    ("Raspberry Pi 5 Modell B, 4GB RAM", "raspberry-pi-5-modell-b-a3029965.html", 118.5),
])
PI_4 = _hits([
    ("Raspberry Pi 5 Modell B, 4GB RAM", "raspberry-pi-5-modell-b-a3029965.html", 118.5),
    ("Raspberry Pi 4 Modell B, 2GB RAM", "raspberry-pi-4-modell-b-a2081127.html", 75.0),
])
SWITCH_24 = _hits([
    ("Ubiquiti UniFiSwitch Pro 24 Rackmount Gigabit Managed Switch, 24x RJ-45, 2x SFP+, 400W PoE++",
     "ubiquiti-unifiswitch-pro-24-rackmount-gigabit-managed-switch-usw-pro-24-poe-a2194138.html", 739.88),
    ("Ubiquiti UniFiSwitch 24 Rackmount Gigabit Managed Switch, 24x RJ-45, 2x SFP, 95W PoE+",
     "ubiquiti-unifiswitch-24-rackmount-gigabit-managed-switch-usw-24-poe-a2189707.html", 406.87),
])
SWITCH_8_150W = _hits([
    ("Ubiquiti UniFiSwitch Enterprise 8 Desktop 2.5G Managed Switch, 8x RJ-45, 2x SFP+, 120W PoE+",
     "ubiquiti-unifiswitch-enterprise-8-desktop-2-5g-managed-switch-usw-enterprise-8-po-a2617724.html", 429.83),
    ("Rackmount.IT Rackmount Kit für Ubiquiti UniFiSwitch 8-150W, 1HE",
     "rackmount-it-rackmount-kit-fuer-ubiquiti-unifiswitch-8-150w-rm-ub-t2-a2448661.html", None),
])
SWITCH_48 = _hits([
    ("Ubiquiti UniFi Pro Max Rackmount Gigabit Smart Switch, 48x RJ-45, 4x SFP+, 720W PoE++", "a1.html", 1329.0),
    ("Ubiquiti UniFiSwitch 48 Rackmount Gigabit Managed Switch, 48x RJ-45, 4x SFP, 195W PoE+", "a2.html", 612.0),
    ("Ubiquiti UniFiSwitch Pro 48 Rackmount Gigabit Managed Switch, 48x RJ-45, 4x SFP+, 600W PoE++", "a3.html", 1118.32),
])
AP_AC_PRO = _hits([
    ("Ubiquiti UAP AC Pro to nanoHD Upgrade Mount, Halterung, 3er-Pack",
     "ubiquiti-uap-ac-pro-to-nanohd-upgrade-mount-nanohd-retrofit-3-a2007967.html", 30.24),
    ("Ubiquiti UniFi AP AC Pro, 5er-Pack", "ubiquiti-unifi-ap-ac-pro-uap-ac-pro-5-a1325759.html", 628.0),
    ("Ubiquiti UniFi AP AC Pro In Wall", "ubiquiti-unifi-ap-ac-pro-in-wall-uap-ac-iw-pro-a1672295.html", None),
    ("Ubiquiti UniFi AP AC Pro", "ubiquiti-unifi-ap-ac-pro-uap-ac-pro-a1325749.html", 146.12),
])
AP_AC_PRO_REAL = AP_AC_PRO[-1:] + AP_AC_PRO[:-1]        # Geizhals' own order: the plain one first, in-wall later
MDT = _hits([
    ("MDT Tasterschnittstelle/-interface BE 2-fach, für 230V Steuersignale, Unterputz, Binäreingang",
     "mdt-tasterschnittstelle-interface-be-2-fach-230v-be-02230-01-a2669150.html", 73.49),
    ("MDT Tasterschnittstelle/-interface BE 2-fach, für potentialfreie Kontakte, Unterputz, Binäreingang",
     "mdt-tasterschnittstelle-interface-be-2-fach-be-02001-02-a2669151.html", 34.19),
])


def _best(title, hits):
    return geizhals.best_hit(hits, deals.geizhals_query(title))


@pytest.mark.parametrize("title, hits, expected", [
    ("iPhone 16 Pro", IPHONE_16_PRO, "Apple iPhone 16 Pro 256GB Titan Weiß"),         # was the plain iPhone 16
    ("I-Phone 14 Pro - 128 GB + 14 Hüllen gratis", IPHONE_14_PRO, "Apple iPhone 14 Pro 128GB dunkellila"),   # was none
    ("Rasperry Pi 5 4GB Active Cooler Set", PI_5, "Raspberry Pi 5 Modell B, 4GB RAM"),  # typo, bundle, compute module
    ("(verkauft) Raspberry Pi 4 Model B Rev 1.5", PI_4, "Raspberry Pi 4 Modell B, 2GB RAM"),
    ("Ubiquiti UniFiSwitch 24 Gigabit Managed PoE Switch, 24x RJ-45, 2x SFP USW-24-POE", SWITCH_24,
     "Ubiquiti UniFiSwitch 24 Rackmount Gigabit Managed Switch, 24x RJ-45, 2x SFP, 95W PoE+"),   # was the Pro 24
    ("Ubiquiti UniFi USW Pro PoE 48 Port Switch", SWITCH_48,
     "Ubiquiti UniFiSwitch Pro 48 Rackmount Gigabit Managed Switch, 48x RJ-45, 4x SFP+, 600W PoE++"),  # was 16-port
    ("Ubiquiti UniFi AP AC Pro (UAP-AC-PRO)", AP_AC_PRO, "Ubiquiti UniFi AP AC Pro"),  # no mount, pack or in-wall
    ("UniFi AC-PRO Access Point", AP_AC_PRO_REAL, "Ubiquiti UniFi AP AC Pro"),          # had no query at all
    ("MDT KNX Tasterschnittstelle 2-fach BE-02001.02 - Unterputz", MDT,
     "MDT Tasterschnittstelle/-interface BE 2-fach, für potentialfreie Kontakte, Unterputz, Binäreingang"),
    ("Ubiquiti UniFi Switch 8 150W (US-8-150W), 8x Gigabit PoE+, 2x SFP", SWITCH_8_150W, None),   # was the Lite 8
    ("UNIFI Switch 48 500W", SWITCH_48, None),                                          # was the 195 W one
])
def test_real_cases(title, hits, expected):
    best = _best(title, hits)
    assert (best.name if best else None) == expected


@pytest.mark.parametrize("token, code", [
    ("USW-24-POE", True), ("UAP-AC-PRO", True), ("AC-PRO", True), ("UXG-Lite", True), ("BE-02001.02", True),
    ("18V-55", True), ("RJ-45", False), ("Wi-Fi", False), ("WLAN-Repeater", False), ("I-Phone", False),
    ("2-fach", False), ("USB-C", False),
])
def test_order_codes(token, code):
    assert geizhals.is_code(token) is code


def test_variant_words_on_both_sides():
    assert not geizhals.matches("Ubiquiti UniFiSwitch 48 Rackmount Gigabit Managed Switch, 48x RJ-45, 4x SFP, 195W PoE+",
                                "Ubiquiti UniFi Pro PoE 48")                      # Pro asked, not in the name
    assert not geizhals.matches("Apple iPhone 15 Pro Max 256GB", "iPhone 15 Pro")    # Max in the name, not asked
    assert geizhals.matches("Apple iPhone 15 Pro 128GB Titan Natur", "iPhone 15 Pro")


def test_codes_are_whole_runs_of_words():
    assert geizhals.matches("Bosch Professional GSR 18V-55 Akku-Bohrschrauber solo", "Bosch GSR 18V-55")
    assert not geizhals.matches("Bosch Professional GSR 18V-550 Akku-Bohrschrauber", "Bosch GSR 18V-55")
    assert geizhals.matches("Ubiquiti UniFi AP AC Pro", "Ubiquiti UniFi Pro UAP-AC-PRO",
                            GH + "ubiquiti-unifi-ap-ac-pro-uap-ac-pro-a1325749.html")       # the code is in the link
    assert not geizhals.matches("Ubiquiti UniFi AP AC Pro", "Ubiquiti UniFi Pro UAP-AC-PRO")  # ... and only there


def test_queries():
    q = deals.geizhals_query
    assert q("iPhone 16 Pro") == "iPhone 16 Pro"                                   # the variant word stays
    assert q("Ubiquiti UniFi USW Pro PoE 48 Port Switch") == "Ubiquiti UniFi Pro PoE 48"
    assert q("UNIFI Switch 48 500W") == "UNIFI Switch 48 500W"                      # wattage makes the variant
    assert q("4x Ubiquiti UniFi AP AC Pro (UAP-AC-PRO)") == "Ubiquiti UniFi Pro UAP-AC-PRO"   # count left out
    assert q("UniFi XG Switch US-16-XG 16-Port 10G") == "UniFi XG Switch US-16-XG"
    assert q("Ubiquiti Unifi Gateway UXG-Lite") == "Ubiquiti Unifi Gateway UXG-Lite"
    assert q("KNX SMART HOME") is None and q("Profi Werkzeugkoffer Set") is None   # no model, no brand: too generic
    assert q("Unifi USG Security Gateway") == "Unifi USG Security Gateway"           # a brand's product by its name
    assert q("Ubiquiti UniFi Cloud Gateway Max + Samsung 980 SSD 500 GB") == "Ubiquiti UniFi Cloud Gateway Max"
    assert q("2 × Ubiquiti UniFi AP AC Pro WLAN Access Points") == "Ubiquiti UniFi AP AC Pro"   # extras, counts out
    assert q("Lot 2 bidons cyclisme Polti") is None and q("Ubiquiti UniFi U6+  NEU versiegelt") == "Ubiquiti UniFi U6+"
    assert q("Raspberry PI4, Joy-it, Aluminiumgehäuse, Schwarz") == "Raspberry PI4 Aluminiumgehäuse"   # a case for it
    assert q("iPhone 13 128GB Display kaputt") == "iPhone 13 128GB"                  # a broken phone's part: no case


def test_search_queries():
    assert geizhals.search_queries("Ubiquiti UniFiSwitch 24 USW-24-POE") == [
        "USW-24-POE", "Ubiquiti UniFiSwitch 24 USW-24-POE", "Ubiquiti UniFiSwitch 24"]   # the code alone first
    assert geizhals.search_queries("Bosch GSR 18V-55") == ["Bosch GSR 18V-55", "Bosch GSR"]   # not a unique code
    assert geizhals.search_queries("iPhone 16 Pro") == ["iPhone 16 Pro"]


def _card(name, url, price):
    return (f'<article class="galleryview__item card"><h3 class="galleryview__name"><a href="{url}" '
            f'class="galleryview__name-link">{name}</a></h3><span class="price">€ {price}</span></article>')


async def test_lookup_searches_again_until_something_fits(monkeypatch):
    seen = []
    pages = {"USW-24-POE": _card(SWITCH_24[0].name, SWITCH_24[0].url, "739,88"),
             "Ubiquiti UniFiSwitch 24 USW-24-POE": _card(SWITCH_24[1].name, SWITCH_24[1].url, "406,87")}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get("fs") or request.url.path)
        if request.url.path == "/":
            return httpx.Response(200, text=pages.get(request.url.params["fs"], ""))
        return httpx.Response(200, text=f"<h1>{SWITCH_24[1].name}</h1><script>x={{lowPrice: 406.87}}</script>")

    monkeypatch.setattr(geizhals, "_transport", httpx.MockTransport(handler))
    monkeypatch.setattr(geizhals, "_limiter", geizhals.RateLimiter(0, name="Geizhals"))
    res = await geizhals.lookup("Ubiquiti UniFiSwitch 24 USW-24-POE")
    assert seen == ["USW-24-POE", "Ubiquiti UniFiSwitch 24 USW-24-POE",     # the code found only the Pro 24
                    "/ubiquiti-unifiswitch-24-rackmount-gigabit-managed-switch-usw-24-poe-a2189707.html"]
    assert res["verified"] and res["match"]["price_from"] == 406.87 and len(res["candidates"]) == 2

    seen.clear()
    res = await geizhals.lookup("Ubiquiti UniFiSwitch 24 USW-24-POE", page=False)   # a price lookup: no page
    assert seen == ["USW-24-POE", "Ubiquiti UniFiSwitch 24 USW-24-POE"] and res["match"]["price_from"] == 406.87

    seen.clear()
    pages.clear()
    pages["Ubiquiti UniFiSwitch 24"] = _card(SWITCH_24[1].name, SWITCH_24[1].url, "406,87")
    res = await geizhals.lookup("Ubiquiti UniFiSwitch 24 USW-24-POE", page=False)
    assert seen == ["USW-24-POE", "Ubiquiti UniFiSwitch 24 USW-24-POE", "Ubiquiti UniFiSwitch 24"]   # nothing at all yet
    assert res["verified"]

    seen.clear()
    pages.clear()
    pages["USW-24-POE"] = _card(SWITCH_24[0].name, SWITCH_24[0].url, "739,88")
    res = await geizhals.lookup("Ubiquiti UniFiSwitch 24 USW-24-POE", page=False)
    assert seen == ["USW-24-POE", "Ubiquiti UniFiSwitch 24 USW-24-POE"]     # something found, nothing fits: no 3rd
    assert not res["verified"]


async def test_a_block_pauses_geizhals(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request.url)
        return httpx.Response(429)

    monkeypatch.setattr(geizhals, "_transport", httpx.MockTransport(handler))
    monkeypatch.setattr(geizhals, "_limiter", geizhals.RateLimiter(0, name="Geizhals"))
    with pytest.raises(geizhals.GeizhalsError, match="429"):
        await geizhals.lookup("iPhone 16 Pro")
    assert geizhals._limiter.cooling_down_s > 0
    with pytest.raises(geizhals.GeizhalsError, match="Geizhals pause"):           # no request while it pauses
        await geizhals.lookup("iPhone 15 Pro")
    assert len(calls) == 1


@pytest.mark.parametrize("name, query, ok", [
    # words are words: "Lot" is not in "Merlotglas", "de" not a word worth counting ("Eau de Parfum")
    ("Riedel The O Wine Tumbler Cabernet/Merlotglas, 2 Stück", "Lot 2", False),
    ("Charlotte Meentzen 4 Men Eau de Parfum, 50ml", "lot de 4", False),
    # glued and abbreviated models, the Plus, ordinals, port counts, a brand Geizhals leaves out, a split brand
    ("Raspberry Pi 4 Modell B, 4GB RAM", "Rapsberry Pi4 4gb", True),
    ("Amazon Echo Show 8 3. Generation schwarz", "Amazon Echo Show8 3", True),
    ("Amazon Echo Show 8 3. Generation schwarz", "Amazon Echo Show 8 3rd", True),
    ("Ubiquiti UniFi 6 Mesh", "Ubiquiti UniFi U6 Mesh", True),
    ("Ubiquiti UniFi 6+", "Ubiquiti UniFi U6+", True),
    ("Ubiquiti UniFi 6+", "Ubiquiti UniFi U6 Plus", True),
    ("Ubiquiti UniFi 6+", "Ubiquiti UniFi U6 Mesh", False),
    ("Ubiquiti UniFi 6 Mesh", "Ubiquiti UniFi U6+", False),
    ("Ubiquiti UniFi 7 Pro XG", "UniFi U7 Pro", False),
    ("Ubiquiti UniFi Pro Max Rackmount Gigabit Smart Switch, 24x RJ-45, 2x SFP+, 400W PoE++", "Ubiquiti UniFi Pro Max 24",
     True),
    ("Ubiquiti UNAS 2 schwarz, 4GB RAM, 1x RJ-45 2.5GBase-T", "UniFi UNAS 2", True),
    ("Esylux RC 230i silber, Bewegungsmelder", "ESY LUX RC 230i", True),
    ("Raspberry Pi 4 Modell B, 4GB RAM", "Raspberry PI4 Aluminiumgehäuse", False),     # the board is no case
    # no number or code to go by: every word but a brand ("UDM" found the 1654 € "Dream Machine Beast")
    ("Ubiquiti UniFi Dream Machine Beast Rackmount 10G Router", "Ubiquiti UniFi Dream Machine UDM", False),
    ("Ubiquiti Cloud Gateway Max 2.5G Smart Router, 5x RJ-45, ohne SSD", "Ubiquiti UniFi Cloud Gateway Max", True),
])
def test_matching_words_models_and_brands(name, query, ok):
    assert geizhals.matches(name, query) is ok
