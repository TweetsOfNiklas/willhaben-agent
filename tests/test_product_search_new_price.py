"""Produktsuche: the Geizhals new price of every offer after the ranking (the price check's lookup, one per product
name), the discount on it, and the order by discount on the dashboard and in the Telegram Mini App."""
import re

import pytest
from fastapi.testclient import TestClient

from app.db import ProductSearch, session, utcnow
from app.services import deals, geizhals
from app.services import product_search as ps
from tests.test_product_search import _clean, claude, wh  # noqa: F401  (pytest fixtures)

GH = {"bosch sms46": {"name": "Bosch Serie 4 SMS46", "url": "https://geizhals.at/a1", "price_from": 400.0},
      "raspberry pi 5 8gb": {"name": "Raspberry Pi 5 8GB", "url": "https://geizhals.at/a2", "price_from": 80.0}}


@pytest.fixture
def lookups(monkeypatch):
    asked = []

    async def match(query, raise_errors=False):
        asked.append(query)
        if query == "fail":
            raise geizhals.GeizhalsError("blocked")
        return GH.get(query.lower())
    monkeypatch.setattr(deals, "geizhals_match", match)
    monkeypatch.setattr(deals, "stored_geizhals", lambda ref: None)
    return asked


def _row(results, status="prices"):
    with session() as db:
        row = ProductSearch(description="Geschirrspüler", status=status, settings={"platforms": ["willhaben"]},
                            plan={"queries": [{"keyword": "Geschirrspüler"}]}, results=results,
                            requests=1, candidates=len(results), started_at=utcnow())
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id


def _r(ad_id, title, price, fit, **kw):
    return {"ad_id": ad_id, "platform": "willhaben", "ref": ad_id, "title": title, "price": price, "fit": fit,
            "url": f"https://x/{ad_id}", **kw}


async def test_one_lookup_per_product_name_and_the_discount(lookups):
    results = [_r("1", "Bosch SMS46 Geschirrspüler", 300.0, 90), _r("2", "Bosch SMS46 neuwertig", 100.0, 80),
               _r("3", "Siemens Geschirrspüler", 150.0, 70), _r("4", "Raspberry Pi 5 8GB", 100.0, 60),
               _r("5", "schon gefragt", 10.0, 50, geizhals={})]
    sid = _row(results)
    await ps.geizhals_prices(sid, results)
    assert lookups == ["Bosch SMS46", "Raspberry Pi 5 8GB"]                   # no product name, asked before: none
    assert [ps.discount(r) for r in results] == [25, 75, None, -25, None]
    assert results[2]["geizhals"] == {} and results[3]["geizhals"]["url"] == "https://geizhals.at/a2"
    row = ps.get(sid)
    assert row.results[0]["geizhals"]["price_from"] == 400.0 and "prices" not in row.progress


async def test_a_geizhals_failure_leaves_the_rest_not_asked(lookups, monkeypatch):
    monkeypatch.setattr(ps.deals, "geizhals_query", lambda title: title)
    results = [_r("1", "fail", 10.0, 90), _r("2", "bosch sms46", 300.0, 80)]
    await ps.geizhals_prices(_row(results), results)
    assert lookups == ["fail"] and all("geizhals" not in r for r in results)


async def test_lookups_are_capped_best_offers_first(lookups, monkeypatch):
    monkeypatch.setattr(ps, "MAX_NEW_PRICES", 2)
    monkeypatch.setattr(ps.deals, "geizhals_query", lambda title: title)
    results = [_r(str(i), f"p{i}", 10.0, 90 - i) for i in range(4)]
    await ps.geizhals_prices(_row(results), results)
    assert lookups == ["p0", "p1"] and "geizhals" not in results[2]


def test_the_lists_sort_by_discount(lookups):
    from app.main import app
    results = [_r("1", "Wenig Rabatt", 300.0, 90, geizhals=GH["bosch sms46"]),
               _r("2", "Ohne Neupreis", 50.0, 80, geizhals={}),
               _r("3", "Viel Rabatt", 100.0, 70, geizhals=GH["bosch sms46"])]
    sid = _row(results, status="done")
    with TestClient(app) as c:
        web = c.get(f"/deals/search/{sid}").text.replace("\u00a0", " ").replace("&nbsp;", " ")
        assert "sort('discount')" in web and 'data-off="-25"' in web and 'data-off="-75"' in web and 'data-off=""' in web
        assert "25 % below the new price" in web and 'href="https://geizhals.at/a1"' in web
        tg = c.get(f"/tg/search/{sid}?order=discount").text
        assert tg.index("Viel Rabatt") < tg.index("Wenig Rabatt") < tg.index("Ohne Neupreis")
        assert "75 % below the new price" in tg
        c.get(f"/tg/search/{sid}?order=fit")


def test_the_price_step_shows_its_progress(lookups):
    from app.main import app
    sid = _row([_r("1", "Bosch SMS46", 300.0, 90)])
    ps._active.add(sid)
    try:
        ps._update(sid, progress={"prices": {"done": 2, "total": 5}})
        with TestClient(app) as c:
            ps._active.add(sid)                         # the app's startup ends running searches
            ps._update(sid, status="prices")
            assert "new prices of the offers: 2 of 5 products" in c.get(f"/deals/search/{sid}").text
            assert "new prices of the offers: 2 of 5 products" in c.get(f"/tg/search/{sid}").text
    finally:
        ps._active.discard(sid)


def test_an_interrupted_price_step_keeps_the_offers():
    sid = _row([_r("1", "Bosch SMS46", 300.0, 90)])
    ps.recover()
    row = ps.get(sid)
    assert row.status == "done" and row.error is None


async def test_an_older_search_looks_its_new_prices_up_when_opened(lookups):
    """A search stored before the step (no "geizhals" on its offers): opening it looks them up in the background."""
    from tests.test_product_search import _finish
    results = [_r("1", "Bosch SMS46 Geschirrspüler", 300.0, 90), _r("2", "Bosch SMS46", 100.0, 80)]
    sid = _row(results, status="done")
    assert ps.fill_new_prices(sid) is True and ps.backfilling(sid)
    assert ps.get(sid).status == "done" and ps.running() is None              # blocks no new search, no other page
    assert ps.fill_new_prices(sid) is False                                   # already on it
    with pytest.raises(ps.Busy):
        ps.rerun(sid)                                                         # this search's own re-run waits
    await _finish()
    row = ps.get(sid)
    assert row.status == "done" and [ps.discount(r) for r in row.results] == [25, 75] and not ps.backfilling(sid)
    assert ps.fill_new_prices(sid) is False and lookups == ["Bosch SMS46"]   # all asked: nothing more


def test_nothing_priced_means_no_discount_order(lookups):
    from app.main import app
    sid = _row([_r("1", "Billig", 50.0, 90, geizhals={}), _r("2", "Teuer", 90.0, 70, geizhals={})], status="done")
    with TestClient(app) as c:
        web = c.get(f"/deals/search/{sid}").text
        assert "priced: false" in web and "Geizhals knows the new price of none" in web
        tg = c.get(f"/tg/search/{sid}?order=discount").text
        assert 'data-no-discount' in tg and tg.index("Billig") < tg.index("Teuer")
        c.get(f"/tg/search/{sid}?order=fit")


def test_the_searchs_geizhals_rule_is_the_agents():
    assert "gh_level" not in ps.make_settings() and "gh_pct" not in ps.make_settings()     # off: nothing stored
    assert ps.make_settings(gh_level="cheap", gh_pct=40)["gh_level"] == "cheap" and "gh_pct" not in ps.make_settings(gh_level="cheap")
    assert ps.make_settings(gh_pct=140)["gh_pct"] == 95 and "gh_level" not in ps.make_settings(gh_level="bogus")
    st = {"gh_pct": 50}
    assert not ps.geizhals_passes(_r("1", "x", 300.0, 90, geizhals=GH["bosch sms46"]), st)      # 25 % below
    assert ps.geizhals_passes(_r("2", "x", 200.0, 90, geizhals=GH["bosch sms46"]), st)          # 50 % below
    assert ps.geizhals_passes(_r("3", "x", 300.0, 90, geizhals={}), st)                         # Geizhals knows none
    assert ps.geizhals_passes(_r("4", "x", 300.0, 90, geizhals=GH["bosch sms46"]), {})


def test_offers_above_the_limit_fold_away(lookups, monkeypatch):
    from app.main import app
    results = [_r("1", "Wenig Rabatt", 300.0, 90, geizhals=GH["bosch sms46"]),
               _r("2", "Ohne Neupreis", 50.0, 80, geizhals={}),
               _r("3", "Viel Rabatt", 100.0, 70, geizhals=GH["bosch sms46"])]
    sid = _row(results, status="done")
    ps._update(sid, settings={"platforms": ["willhaben"], "gh_pct": 50})
    with TestClient(app) as c:
        web = c.get(f"/deals/search/{sid}").text
        out = web[web.index("data-ps-gh-out"):]
        assert "1 fitting offer above your Geizhals limit" in out and "Wenig Rabatt" in out
        assert web.index("Viel Rabatt") < web.index("data-ps-gh-out") and "50 % below Geizhals" in web
        tg = c.get(f"/tg/search/{sid}").text
        assert tg.index("data-ps-gh-out") < tg.index("Wenig Rabatt") and "Best offers · 2" in tg
        started = []
        monkeypatch.setattr(ps, "start", lambda d, st: started.append(st) or sid)
        c.post("/deals/search", data={"description": "x", "gh_level": "pct", "gh_pct": "40"}, follow_redirects=False)
        c.post("/deals/search", data={"description": "x", "gh_level": "fair", "gh_pct": "40"}, follow_redirects=False)
        c.post("/deals/search", data={"description": "x", "gh_level": ""}, follow_redirects=False)
        assert [(s.get("gh_level"), s.get("gh_pct")) for s in started] == [(None, 40), ("fair", None), (None, None)]
        form = c.get(f"/deals?from={sid}").text
        assert 'name="gh_level"' in form and 'value="pct" selected' in form and 'name="gh_pct" x-model="ghPct" min="1" max="95" step="1" value="50"' in form


async def test_claudes_product_name_comes_first(lookups):
    """The product Claude names while ranking is what Geizhals is asked; "" from Claude: a generic offer, no lookup
    (the title's name would find some other product); the title's name only for an offer Claude did not rank."""
    cards = [_r("1", "8GB Raspberry Pi 5 + Kühlgehäuse +256gb Micro SD", 120.0, None),
             _r("2", "Bosch SMS46 Geschirrspüler", 300.0, None)]
    merged = ps.merge_ranking({"results": [{"nr": 1, "fit": 90, "reason": "", "red_flags": [], "product": " Raspberry Pi 5\n8GB "},
                                           {"nr": 2, "fit": 80, "reason": "", "red_flags": [], "product": ""}]}, cards)
    assert [r["product"] for r in merged] == ["Raspberry Pi 5 8GB", ""]
    unranked = _r("3", "Bosch SMS46 Geschirrspüler", 300.0, None)
    rows = merged + [unranked]
    await ps.geizhals_prices(_row(rows), rows)
    assert lookups == ["Raspberry Pi 5 8GB", "Bosch SMS46"] and [ps.discount(r) for r in rows] == [-50, None, 25]
    assert merged[1]["geizhals"] == {}


def test_the_ranking_asks_for_the_product():
    from app.llm.prompts import SEARCH_RANK_SCHEMA, SEARCH_RANK_SYSTEM
    item = SEARCH_RANK_SCHEMA["properties"]["results"]["items"]
    assert "product" in item["required"] and "Geizhals" in SEARCH_RANK_SYSTEM


async def test_the_sum_of_the_parts(monkeypatch):
    found = {"cpu": 100.0, "gpu": 150.5, "ram": None, "ssd": 40.0}

    async def match(q, raise_errors=False):
        return {"name": q.upper(), "url": f"https://geizhals.at/{q}", "price_from": found[q]} if found.get(q) else None
    monkeypatch.setattr(deals, "geizhals_match", match)
    res = await deals.parts_new_price(["cpu", "gpu", "ram", "ssd", " ", "cpu"])
    assert res["price_from"] == 290.5 and res["missing"] == ["ram"] and res["name"] == "CPU + GPU + SSD"
    assert await deals.parts_new_price(["ram", "x", "y", "cpu"]) is None          # fewer than half priced


async def test_the_product_search_sums_an_offers_parts(lookups, monkeypatch):
    async def parts(names):
        lookups.append(tuple(names))
        return {"name": " + ".join(names), "url": "https://geizhals.at/p", "price_from": 400.0,
                "parts": [{"query": n, "name": n, "url": "u", "price_from": 200.0} for n in names], "missing": []}
    monkeypatch.setattr(deals, "parts_new_price", parts)
    cards = [_r("1", "PC i7 GTX", 100.0, None), _r("2", "noch ein PC", 300.0, None)]
    merged = ps.merge_ranking({"results": [{"nr": i, "fit": 90 - i, "reason": "", "red_flags": [], "product": "",
                                            "parts": ["Intel Core i7-3770K", "GTX 1050"]} for i in (1, 2)]}, cards)
    await ps.geizhals_prices(_row(merged), merged)
    assert lookups == [("Intel Core i7-3770K", "GTX 1050")]                   # the same parts: asked once
    assert [ps.discount(r) for r in merged] == [75, 25] and len(merged[0]["geizhals"]["parts"]) == 2


def test_a_new_price_the_comparables_contradict_is_no_discount():
    r = _r("1", "Mini PC Ryzen 7", 450.0, 90, geizhals={"name": "HP EliteDesk", "url": "u", "price_from": 1383.45},
           stats={"median": 85.0})
    assert ps.discount(r) is None
    assert ps.discount({**r, "stats": {"median": 900.0}}) == 67


async def test_load_more_does_not_rank_what_is_shown_already(wh, claude, monkeypatch):  # noqa: F811
    """An interrupted "load more" stored some of its batch already: the next one skips those offers."""
    from tests.test_product_search import _finish
    monkeypatch.setattr(ps, "MAX_CANDIDATES", 4)
    monkeypatch.setattr(ps, "MAX_RANKED", 4)
    sid = ps.start("Leiser Geschirrspüler", ps.make_settings(photos=False))
    await _finish()
    row = ps.get(sid)
    shown = row.pool[0]
    ps._update(sid, results=list(row.results) + [{**shown, "fit": 50, "reason": "", "red_flags": []}])
    assert ps.load_more(sid)
    await _finish()
    keys = [ps._key(r) for r in ps.get(sid).results]
    assert len(keys) == len(set(keys))


def test_restart_keeps_partly_ranked_offers():
    sid = _row([_r("1", "Bosch SMS46", 300.0, 90)], status="ranking")
    ps.recover()
    row = ps.get(sid)
    assert row.status == "done" and "restart" in row.warning and row.results


def test_the_mismatch_rule_also_spares_the_geizhals_limit():
    r = _r("1", "Mini PC", 200.0, 90, geizhals={"name": "Zubehör", "url": "u", "price_from": 40.0}, stats={"median": 250.0})
    assert ps.discount(r) is None and ps.geizhals_passes(r, {"gh_pct": 30})


async def test_a_part_never_takes_a_whole_systems_price(monkeypatch):
    """"NVIDIA GeForce RTX 3060" found a gaming PC on Geizhals: the lookup's other results give the card itself."""
    async def match(q, raise_errors=False):
        if "RTX" in q:
            return {"name": "Komplettsystem, AMD Ryzen 5 5500, 16GB, NVIDIA GeForce RTX 3060", "url": "https://g/pc",
                    "price_from": 989.0}
        return {"name": q, "url": f"https://g/{len(q)}", "price_from": 100.0}
    monkeypatch.setattr(deals, "geizhals_match", match)
    monkeypatch.setattr(deals, "geizhals_candidates", lambda q: [
        {"name": "Komplettsystem, Intel Core i5, NVIDIA GeForce RTX 3060", "url": "https://g/pc2", "price": 899.0},
        {"name": "Gainward GeForce RTX 3060 Ghost, 12GB GDDR6", "url": "https://g/card", "price": 289.0}])
    monkeypatch.setattr(deals, "_matches", lambda name, q, url: "RTX 3060" in name)
    res = await deals.parts_new_price(["NVIDIA GeForce RTX 3060 12GB", "AMD Ryzen 5 5600"])
    assert [p["price_from"] for p in res["parts"]] == [289.0, 100.0] and res["price_from"] == 389.0


def test_discount_sorts_folded_offers_in_when_no_shown_one_has_a_new_price(lookups):
    """10-26: every offer with a new price was above the search's Geizhals limit (folded), so "Discount" only said
    there was nothing to sort. It now sorts those in – shown in that order only, marked as above the limit."""
    from app.main import app
    sid = _row([_r("1", "Wenig Rabatt", 300.0, 90, geizhals=GH["bosch sms46"]),
                _r("2", "Ohne Neupreis", 50.0, 80, geizhals={}),
                _r("3", "Ohne Neupreis zwei", 60.0, 70, geizhals={})], status="done")
    ps._update(sid, settings={"platforms": ["willhaben"], "gh_pct": 50})
    with TestClient(app) as c:
        web = c.get(f"/deals/search/{sid}").text
    assert "priced: true" in web and "data-discount-over-limit" in web
    tag = re.search(r"<article[^>]*data-over-limit[^>]*>", web).group(0)
    assert 'data-off="-25"' in tag and "x-show=\"order === 'discount'\"" in tag, tag
    assert "Above your Geizhals limit" in web[web.index(tag):web.index(tag) + 3000]
    assert web.index("data-over-limit") < web.index("data-ps-gh-out")          # in the list, and still folded below
    with TestClient(app) as c:
        tg = c.get(f"/tg/search/{sid}?order=discount").text
        c.get(f"/tg/search/{sid}?order=fit")
    assert "data-no-discount" not in tg and "data-discount-over-limit" in tg and "data-over-limit" in tg
    assert tg.index("Wenig Rabatt") < tg.index("Ohne Neupreis")              # with a new price first
