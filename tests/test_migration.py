"""A database from before there were other platforms keeps working: old rows become willhaben's."""
from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import create_engine, select

from app import db as dbmod
from app.db import AccessCount, Hit, SearchAgent, WhRecording


OLD_DDL = [
    """CREATE TABLE searchagent (id INTEGER PRIMARY KEY, name VARCHAR NOT NULL, source_url VARCHAR NOT NULL,
       webapi_url VARCHAR NOT NULL, interval_s INTEGER NOT NULL, enabled BOOLEAN NOT NULL, notify BOOLEAN NOT NULL,
       notify_price_drops BOOLEAN NOT NULL, baseline_done BOOLEAN NOT NULL, created_at DATETIME NOT NULL)""",
    """CREATE TABLE hit (id INTEGER PRIMARY KEY, agent_id INTEGER NOT NULL, ad_id VARCHAR NOT NULL, heading VARCHAR NOT NULL,
       url VARCHAR NOT NULL, first_seen_at DATETIME NOT NULL, last_seen_at DATETIME NOT NULL, state VARCHAR NOT NULL,
       bumped_count INTEGER NOT NULL, UNIQUE (agent_id, ad_id))""",
    """CREATE TABLE accesscount (minute INTEGER NOT NULL, channel VARCHAR NOT NULL, kind VARCHAR NOT NULL,
       outcome VARCHAR NOT NULL, count INTEGER NOT NULL, PRIMARY KEY (minute, channel, kind, outcome))""",
    """CREATE TABLE whrecording (id INTEGER PRIMARY KEY, ts DATETIME NOT NULL, channel VARCHAR NOT NULL, kind VARCHAR NOT NULL,
       method VARCHAR NOT NULL, url VARCHAR NOT NULL, canonical_key VARCHAR NOT NULL, outcome VARCHAR NOT NULL,
       cached BOOLEAN NOT NULL, req_size INTEGER NOT NULL, req_truncated BOOLEAN NOT NULL, resp_size INTEGER NOT NULL,
       resp_truncated BOOLEAN NOT NULL, pruned BOOLEAN NOT NULL)""",
]


def test_old_rows_become_willhabens():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    with engine.begin() as conn:
        for ddl in OLD_DDL:
            conn.execute(text(ddl))
        conn.execute(text("INSERT INTO searchagent VALUES (1, 'Rad', 'https://www.willhaben.at/iad/x?keyword=rad', "
                          "'https://www.willhaben.at/webapi/x', 300, 1, 1, 1, 1, '2026-10-01 10:00:00')"))
        conn.execute(text("INSERT INTO hit VALUES (1, 1, '123', 'Rad', 'https://www.willhaben.at/iad/d/rad-123/', "
                          "'2026-10-01 10:00:00', '2026-10-01 10:00:00', 'new', 0)"))
        conn.execute(text("INSERT INTO accesscount VALUES (29000000, 'anonymous', 'search', 'ok', 7)"))
        conn.execute(text("INSERT INTO whrecording VALUES (1, '2026-10-01 10:00:00', 'anonymous', 'search', 'GET', "
                          "'https://www.willhaben.at/webapi/x', 'k', 'ok', 0, 0, 0, 0, 0, 0)"))
    dbmod.set_engine(engine)
    dbmod.init_db()
    with dbmod.session() as db:
        a = db.get(SearchAgent, 1)
        assert a.platform == "willhaben" and a.spec is None and a.webapi_url.startswith("https://www.willhaben.at/webapi")
        assert db.get(Hit, 1).platform == "willhaben"
        rows = list(db.exec(select(AccessCount)))
        assert [(r.minute, r.platform, r.channel, r.count) for r in rows] == [(29000000, "willhaben", "anonymous", 7)]
        assert db.get(WhRecording, 1).platform is None
        db.add(AccessCount(minute=29000000, platform="kleinanzeigen", channel="anonymous", kind="search", outcome="ok", count=1))
        db.add(SearchAgent(name="k", platform="kleinanzeigen", source_url="x", webapi_url="x", spec={"platform": "kleinanzeigen", "url": "x"}))
        db.commit()
        assert {r.platform for r in db.exec(select(AccessCount))} == {"willhaben", "kleinanzeigen"}
    dbmod.init_db()                                                   # a second start changes nothing
    with dbmod.session() as db:
        assert len(list(db.exec(select(AccessCount)))) == 2
