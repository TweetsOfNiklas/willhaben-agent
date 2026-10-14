"""Pages join optional details without leaving empty separators behind."""
from app.db import Hit, SearchAgent, session
from tests.test_dashboard import client  # noqa: F401  (pytest fixture)


def test_home_and_agents_skip_missing_details(client):  # noqa: F811
    with session() as db:
        a = SearchAgent(name="Echo", source_url="https://www.willhaben.at/iad/x", webapi_url="https://www.willhaben.at/webapi/x",
                        queries=[{"keyword": "", "platform": "willhaben", "spec": {}}, {"keyword": "Echo Show 8", "platform": "vinted", "spec": {}}])
        db.add(a)
        db.commit()
        db.add(Hit(agent_id=a.id, ad_id="1", platform="vinted", state="new", heading="Écho show 8", url="https://www.vinted.at/items/1",
                   price=50))                                              # Vinted: no place, no date
        db.commit()
    home = client.get("/").text
    assert "Écho show 8" in home and "· —" not in home and "&middot; —" not in home
    assert "2 searches: Echo Show 8" in client.get("/agents").text         # the empty keyword is left out
