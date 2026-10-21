"""The settings map (app/services/settings_catalog.py): it names every section page, every anchor it points at is an id
in a template, the overview and the search index come from it, and so does the command palette."""
import re
from pathlib import Path

from fastapi.testclient import TestClient

from app import i18n
from app.db import set_setting
from app.main import SETTINGS_SECTIONS, app
from app.services import settings_catalog as sc

TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"


class _Site:
    def __init__(self, id, name):
        self.id, self.name = id, name


SITES = [_Site("kleinanzeigen", "Kleinanzeigen"), _Site("vinted", "Vinted"), _Site("facebook", "Facebook Marketplace")]


def test_catalog_names_every_section_once():
    groups = sc.build(SITES, sc._plain)
    ids = [s.id for g in groups for s in g.sections]
    assert len(ids) == len(set(ids))
    assert [i for i in ids if i not in {s.id for s in SITES}] == list(SETTINGS_SECTIONS)   # the nav order is the page order
    assert ids[:6] == ["platforms", "willhaben", "kleinanzeigen", "vinted", "facebook", "quiet"]   # defaults, then the sites
    assert groups[-1].sections[0].id == "vpn"                                          # the VPN under System
    assert [g.label for g in groups] == ["Platforms", "Search & prices", "Buying", "Selling", "Shipping & pickup", "AI", "Notifications", "System"]
    for g in groups:
        for s in g.sections:
            anchors = [i.anchor for i in s.items]
            assert anchors and len(anchors) == len(set(anchors)), s.id
            assert s.description.endswith("."), s.id


def test_every_anchor_is_an_id_in_a_template():
    """A hit opens /settings/<section>#<anchor>: the id must exist on that page (static check, so that branches a test
    cannot render, such as a connected calendar, count too)."""
    src = "\n".join(p.read_text(encoding="utf-8") for p in TEMPLATES.rglob("*.html"))
    ids = set(re.findall(r'id="([^"{]+)"', src))
    ids |= {f"s-print-{w}" for w in ("label", "normal")} if 'id="s-print-{{ which }}"' in src else set()
    ids |= {f"s-prices-{g}" for g in ("used", "new")} if 'id="s-prices-{{ g }}"' in src else set()
    missing = [(s.id, i.anchor) for g in sc.build(SITES, sc._plain) for s in g.sections for i in s.items if i.anchor not in ids]
    assert not missing, missing


def test_site_pages_carry_their_own_anchors():
    with TestClient(app) as c:
        ka, vi = c.get("/settings/kleinanzeigen").text, c.get("/settings/vinted").text
    assert 'id="s-auth"' in ka and 'id="s-auth"' not in vi and 'id="s-enabled"' in vi and 'id="s-vpn-site"' in vi
    with TestClient(app) as c:
        page = c.get("/settings/shipping").text
    assert 'id="s-ship-zip"' in page and 'id="payment-card"' in page and 'id="s-ship-auto-carriers"' in page


def test_overview_lists_groups_and_what_needs_attention():
    with TestClient(app) as c:
        page = c.get("/settings").text
    assert "<h1>Settings</h1>" in page and "Search a setting, or pick a section." in page
    for s in SETTINGS_SECTIONS:
        assert f'href="/settings/{s}"' in page, s
    assert page.count("Sender address, pickup radius") == 1          # the description, once (the overview)
    # the test server: no sender address, Claude not signed in -> both in "Needs attention"
    att = page[page.index("data-attention"):page.index("</section>", page.index("data-attention"))]
    assert "Sender address missing" in att and "Claude is not signed in." in att and 'href="/settings/claude"' in att
    assert "willhaben: not logged in" not in att                     # the browser is not running: not a problem
    nav = page[page.index('<nav aria-label="Settings"'):]
    assert nav.index('href="/settings"') < nav.index('href="/settings/willhaben"') and 'aria-current="page"' in nav[:400]
    assert 'href="/settings/overview"' not in page
    with TestClient(app) as c:
        assert c.get("/settings/overview").status_code == 404
        sec = c.get("/settings/quiet").text
    assert "<h2>Quiet hours &amp; pace</h2>" in sec and ">Search &amp; prices</div>" in sec and "data-attention" not in sec
    assert "Changes are saved per section." in sec and 'id="s-adaptive"' in sec


def test_search_index_in_the_ui_language_and_english():
    with TestClient(app) as c:
        en = c.get("/settings/search.json").json()["sections"]
        set_setting("ui_lang", "de")
        de = c.get("/settings/search.json").json()["sections"]
    ids = [s["id"] for s in en]
    assert "willhaben" in ids and "vinted" in ids and "language" in ids and len(ids) == len(set(ids))
    ship = next(s for s in en if s["id"] == "shipping")
    assert ship["label"] == ["Shipping address"] and ship["group"] == ["Shipping & pickup"]
    zip_ = next(i for i in ship["items"] if i["anchor"] == "s-ship-zip")
    assert zip_["label"] == ["Postcode"] and zip_["keywords"] == ["sender address, ZIP"]
    ship = next(s for s in de if s["id"] == "shipping")
    assert ship["label"] == ["Versandadresse", "Shipping address"]
    zip_ = next(i for i in ship["items"] if i["anchor"] == "s-ship-zip")
    assert zip_["label"] == ["PLZ", "Postcode"] and zip_["keywords"][0].startswith("Absenderadresse")
    quiet = next(s for s in de if s["id"] == "quiet")
    assert quiet["label"] == ["Ruhezeit & Tempo", "Quiet hours & pace"] and quiet["group"] == ["Suche & Preise", "Search & prices"]
    i18n.set_request_lang("en")


def test_settings_page_has_the_search_and_the_old_hash_links():
    with TestClient(app) as c:
        page = c.get("/settings/claude").text
    assert 'x-data="settingsSearch"' in page and "/settings/search.json" in page and 'placeholder="Search settings…"' in page
    assert "location.replace('/settings/' + id + location.search)" in page     # /settings#claude -> /settings/claude
    assert 'data-settings-hits' in page and "No setting matches" in page


def test_command_palette_lists_every_settings_section():
    with TestClient(app) as c:
        home = c.get("/").text
    items = home[home.index('<template id="cmdk-items">'):home.index("</template>", home.index('<template id="cmdk-items">'))]
    assert "Settings|Overview|/settings\n" in items
    assert "PayLivery|Settings · Buying|/settings/paylivery\n" in items
    assert "Shipping address|Settings · Shipping &amp; pickup|/settings/shipping\n" in items
    assert "Kleinanzeigen|Settings · Platforms|/settings/kleinanzeigen\n" in items
    for s in SETTINGS_SECTIONS:
        assert f"|/settings/{s}\n" in items, s
