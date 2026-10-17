import copy
from datetime import datetime, timezone

import pytest

from app.willhaben.parse import (
    InvalidSearchUrl, describe_search, extract_ad_id, keyword_search_url, parse_detail, parse_search, to_webapi_url,
)
from tests.conftest import load


def test_parse_search_fixture():
    page = parse_search(load("search_fahrrad_rows3.json"))
    assert page.rows_found > 90000
    assert len(page.ads) == 3
    ad = page.ads[0]
    assert ad.ad_id.isdigit()
    assert ad.heading
    assert ad.url.startswith("https://www.willhaben.at/iad/kaufen-und-verkaufen/d/")
    assert ad.url.endswith(f"-{ad.ad_id}/")
    assert ad.image_url and ad.image_url.startswith("https://cache.willhaben.at/mmo/")
    assert isinstance(ad.published_at, datetime)
    assert ad.end_date and ad.end_date > ad.published_at
    assert ad.status == "active"


def test_parse_detail_fixture():
    d = parse_detail(load("detail_1662146981_nextdata.json"))
    assert d.ad_id == "1662146981"
    assert d.heading == "Fahrradhose Gr xs"
    assert d.price == 0
    assert d.price_display == "zu verschenken"
    assert d.status == "active"
    assert d.end_date == datetime(2026, 11, 23, 9, 26, 53, tzinfo=timezone.utc)  # +0100 -> UTC
    assert "Versandkosten" in d.description and "<br" not in d.description
    assert d.seller_name == "Erika"
    assert d.seller_login_id == "10758884"
    assert d.category_path[0] == "Sport / Sportgeräte"
    assert d.images[0].endswith("_hoved.jpg")
    assert d.url.endswith("fahrradhose-gr-xs-1662146981/")


@pytest.mark.parametrize("src,expected", [
    ("https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz?keyword=fahrrad&page=3&sfId=abc",
     "https://www.willhaben.at/webapi/ad-search/search/atz/seo/kaufen-und-verkaufen/marktplatz?keyword=fahrrad&sort=1&rows=30"),
    ("https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz/fahrraeder-4552?PRICE_TO=300&areaId=900&areaId=3",
     "https://www.willhaben.at/webapi/ad-search/search/atz/seo/kaufen-und-verkaufen/marktplatz/fahrraeder-4552?PRICE_TO=300&areaId=900&areaId=3&sort=1&rows=30"),
    ("https://www.willhaben.at/iad/immobilien/mietwohnungen/wien?sort=3",
     "https://www.willhaben.at/webapi/ad-search/search/atz/seo/immobilien/mietwohnungen/wien?sort=1&rows=30"),
])
def test_to_webapi_url(src, expected):
    assert to_webapi_url(src) == expected


@pytest.mark.parametrize("bad", [
    "https://www.example.com/iad/foo",
    "https://www.willhaben.at/iad/kaufen-und-verkaufen/d/fahrradhose-gr-xs-1662146981/",
    "https://www.willhaben.at/will/hilfe",
    "fahrrad",
])
def test_to_webapi_url_rejects(bad):
    with pytest.raises(InvalidSearchUrl):
        to_webapi_url(bad)


def test_helpers():
    assert describe_search("https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz?keyword=ktm+rad") == "ktm rad"
    assert describe_search("https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz/fahrraeder-4552") == "Fahrraeder"
    assert extract_ad_id("1662146981") == "1662146981"
    assert extract_ad_id("https://www.willhaben.at/iad/object?adId=123456789") == "123456789"
    assert extract_ad_id("https://www.willhaben.at/iad/kaufen-und-verkaufen/d/fahrradhose-gr-xs-1662146981/") == "1662146981"
    assert extract_ad_id("nope") is None
    assert "PRICE_TO=100" in keyword_search_url("lampe", price_to=100)


def test_parse_me_nested_userdata():
    from app.willhaben.session import parse_me

    # Shape of the real /userprofile/me response (values made up); profile lives under userData.
    data = {"contextLinkList": {"contextLink": []},
            "taggingData": {"segmentData": {"identify": {"traits": {"id": "x"}}}},
            "userData": {"loginId": 12345678, "uuid": "0b9e-uuid", "firstName": "Max", "lastName": "",
                         "emailAddress": "max@example.at", "orgId": 1}}
    assert parse_me(data) == {"id": "12345678", "uuid": "0b9e-uuid", "nickname": "Max", "email": "max@example.at"}


def test_parse_me_anonymous():
    from app.willhaben.session import parse_me

    assert parse_me(None) is None
    assert parse_me({"contextLinkList": {}, "userData": {"loginId": None}}) is None


def test_web_url_variants():
    from app.willhaben.parse import web_url

    want = "https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz/a/zustand-neu-22?keyword=fahrrad"
    assert web_url("/search/atz/seo/kaufen-und-verkaufen/marktplatz/a/zustand-neu-22?keyword=fahrrad&sort=1&sfId=x&rows=3&isNavigation=true") == want
    assert web_url("https://ad-search.willhaben.at/restapi/v2/search/atz/seo/kaufen-und-verkaufen/marktplatz/a/zustand-neu-22?keyword=fahrrad") == want
    assert web_url("/iad/kaufen-und-verkaufen/marktplatz/a/zustand-neu-22?keyword=fahrrad") == want
    assert web_url("/kaufen-und-verkaufen/marktplatz?rows=3&sort=1") == "https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz"


def test_with_params():
    from app.willhaben.parse import with_params

    u = "https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz?keyword=rad&PRICE_TO=100&areaId=900&areaId=3"
    assert with_params(u, {"PRICE_TO": "800", "PRICE_FROM": None}) == \
        "https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz?keyword=rad&areaId=900&areaId=3&PRICE_TO=800"
    assert with_params(u, {"keyword": None}).endswith("marktplatz?PRICE_TO=100&areaId=900&areaId=3")


def test_parse_filters_fixture():
    from app.willhaben.parse import parse_filters

    f = parse_filters(load("search_fahrrad_rows3.json"), "https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz?keyword=fahrrad")
    assert f.keyword == "fahrrad"
    assert f.url == "https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz?keyword=fahrrad"
    by_id = {flt.id: flt for _, fs in f.groups for flt in fs}
    assert "keyword" not in by_id
    cat = by_id["category"]
    assert cat.kind == "HIERARCHICAL" and len(cat.values) == 19
    assert cat.values[0].label == "Antiquitäten / Kunst" and cat.values[0].hits == 907
    assert cat.values[0].url == "https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz/antiquitaeten-kunst-6941?keyword=fahrrad"
    assert by_id["price"].range_params == ["PRICE_FROM", "PRICE_TO"]
    assert by_id["state"].multi and by_id["state"].values[0].label == "Burgenland"
    assert f.rows_found > 0 and f.ads
    assert f.suggested_name() == "fahrrad"


def test_clean_text_keeps_paragraphs_and_lists():
    from app.willhaben.parse import clean_text

    raw = ("<p><strong>Carbon NEU </strong></p><p><strong>Shimano 105</strong></p><p>Größe M</p>"
           "<ul><li>Rechnung</li><li>Garantie &amp; Versand</li></ul>Zeile 1<br>Zeile 2")
    assert clean_text(raw) == "Carbon NEU\nShimano 105\nGröße M\n\n• Rechnung\n• Garantie & Versand\nZeile 1\nZeile 2"


@pytest.mark.parametrize("options, expected", [
    ([], None),                                              # the live fixture: no PayLivery delivery options
    ([{"price": "4,90"}, {"price": 3.5}], 3.5),              # cheapest of several
    ([{"price": "4,90 €"}, {"price": "1.234,50"}], 4.9),     # German text prices
    ([{"price": "6"}], 6.0),
    ([{"price": 0}], 0.0),                                   # free delivery is a price too
    ([{"price": None}, {"price": "x"}, {"price": True}], None),
    ({"price": "2,50"}, 2.5),                                # a single option, not in a list
])
def test_shipping_price_from_paylivery_delivery_options(options, expected):
    page = copy.deepcopy(load("detail_1662146981_nextdata.json"))
    page["advertDetails"]["p2ppOptions"]["deliveryOptions"] = options
    d = parse_detail(page)
    assert d.shipping_price == expected
    assert d.as_context()["shipping_price"] == expected


def test_shipping_price_none_without_p2pp_options():
    page = copy.deepcopy(load("detail_1662146981_nextdata.json"))
    del page["advertDetails"]["p2ppOptions"]
    assert parse_detail(page).shipping_price is None


def test_shipping_price_of_a_live_paylivery_ad():
    """Recorded on 2026-10-10 from a real PayLivery ad (Post, parcel size M): 7.90 to the door, 6.90 to a pickup
    point (depositoryPrice): the cheaper one is "Versand ab", both are options."""
    from app.willhaben.parse import parse_detail
    from tests.conftest import load

    nd = load("detail_922008415_nextdata.json")
    d = parse_detail(nd["props"]["pageProps"])
    assert d.paylivery_enabled and d.buy_now and d.shipping_price == 6.9 and d.price == 100.0
    assert d.shipping_options == [{"name": "Post", "home": 7.9, "point": 6.9}]


def test_detail_handover_from_the_ad_page():
    from app.willhaben.parse import _handover

    assert parse_detail(load("detail_1662146981_nextdata.json")).shipping == "ship"   # "Selbstabholung", "Versand"
    assert _handover({"advertisingParameters": {"Uebergabe": ["Selbstabholung"]}}) == "pickup"
    assert _handover({"advertisingParameters": {"Uebergabe": ["Selbstabholung"], "payliveryEnabled": ["true"]}}) == "ship"
    assert _handover({"advertisingParameters": {}}) is None
