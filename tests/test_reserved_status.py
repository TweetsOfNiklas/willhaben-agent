"""Reserved ads are marked on the hit from the search list (willhaben's advertStatus, Kleinanzeigen's ad-status PAUSED,
Facebook's is_pending) and from the ad page (Vinted's is_reserved); listed as active again clears it."""
from types import SimpleNamespace

from app.db import Hit
from app.platforms import kleinanzeigen, vinted
from app.services import search_agents


def _ad(status):
    return SimpleNamespace(status=status)


def test_a_reserved_listing_marks_the_hit_and_active_clears_it():
    h = Hit(agent_id=1, platform="willhaben", ad_id="1", heading="x")
    search_agents._list_status(h, _ad("reserved"))
    assert h.site_status == "reserved" and h.status_checked_at
    search_agents._list_status(h, _ad("active"))
    assert h.site_status == "active"
    v = Hit(agent_id=1, platform="vinted", ad_id="2", heading="x", site_status="reserved")
    search_agents._list_status(v, _ad("active"))          # Vinted's list says active for every ad: page decides
    assert v.site_status == "reserved"


def test_kleinanzeigen_paused_is_reserved():
    ad = {"id": {"value": "35"}, "title": {"value": "Switch"}, "ad-status": {"value": "PAUSED"}}
    assert kleinanzeigen.parse_listing(ad).status == "reserved"
    assert kleinanzeigen.parse_listing({**ad, "ad-status": {"value": "ACTIVE"}}).status == "active"
    assert kleinanzeigen.parse_listing({"id": {"value": "36"}, "title": {"value": "x"}}).status == "active"


def test_vinted_listing_flag():
    item = {"productItem": {"id": 5, "title": "Mini-PC", "price": {"amount": "50", "currency_code": "EUR"},
                            "is_reserved": True}}
    assert vinted.parse_listing(item, vinted.HOST).status == "reserved"
    assert vinted._RESERVED.search('{"can_buy":true,"is_reserved":true,"is_hidden":false}')


def test_the_file_database_waits_and_uses_wal(tmp_path, monkeypatch):
    from sqlalchemy import text

    from app import db
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "get_settings", lambda: SimpleNamespace(db_path=tmp_path / "t.db"))
    try:
        with db.get_engine().connect() as c:
            assert c.execute(text("PRAGMA journal_mode")).scalar() == "wal"
            assert c.execute(text("PRAGMA busy_timeout")).scalar() == 30000
    finally:
        db._engine.dispose()


def test_vinted_sold_item_from_its_status_plugin():
    sold = {"buyer_item_status": {"item_id": "1", "theme": "SUCCESS", "title": "Verkauft"}}
    assert vinted._item_status(sold, "") == "sold"
    assert vinted._item_status({"buyer_item_status": {"title": "Reserviert"}}, "") == "reserved"
    assert vinted._item_status({}, '"is_reserved":true') == "reserved"
    assert vinted._item_status({}, '"is_reserved":false') == "active"
