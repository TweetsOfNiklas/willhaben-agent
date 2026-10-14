"""The price section when only the VPN held the comparable searches (a server search runs for that site): it says
so and offers a retry instead of "Comparable search failed"."""
from app.services import deals


def _res(kind: str | None) -> dict:
    comp = {"query": "UniFi UDM SE", "count": 0, "stats": None, "closest": [], "filtered": {},
            "error": "willhaben refused the VPN server es-mad-wg-204 (blocked by the site); its requests pause"}
    if kind:
        comp.update(error_kind=kind, error_site="willhaben")
    return {"ad": {"ad_id": "3540356386", "platform": "kleinanzeigen", "price": 290.0, "title": "UniFi UDM SE"},
            "comparables": comp, "geizhals": None, "position": deals.position(290.0, None, None)}


def _render(res: dict, compact: bool) -> str:
    from app.main import templates

    return templates.get_template("_deal.html").render(res=res, error=None, compact=compact, h=None)


def test_a_vpn_wait_is_no_failure_and_offers_a_retry():
    viewer = _render(_res("vpn"), compact=True)
    assert "the VPN is looking for a server for willhaben" in viewer and "Comparable search failed" not in viewer
    assert 'hx-get="/hits/ad/kleinanzeigen/3540356386/deal"' in viewer and "Try again" in viewer
    page = _render(_res("vpn"), compact=False)
    assert 'href="/deals/ad/kleinanzeigen/3540356386"' in page                # the full page reloads itself
    other = _render(_res(None), compact=True)                                  # any other error: as before
    assert "Comparable search failed" in other and "Try again" not in other
