import json
import os
import tempfile
from pathlib import Path

os.environ.update({
    "DATA_DIR": tempfile.mkdtemp(prefix="wh-test-"),
    "BROWSER_ENABLED": "0",
    "HA_LISTENER": "0",
    "TELEGRAM_LISTENER": "0",
    "HUMANIZE_CHAT": "0",       # auto replies answer immediately in tests (test_humanize covers the timing)
    "TELEGRAM_BOT_TOKEN": "",
    "REQUEST_MIN_GAP_S": "0",
    "HA_URL": "",
    "HA_TOKEN": "",
    "HA_NOTIFY_SERVICE": "",
    "CLAUDE_CODE_OAUTH_TOKEN": "",
    "DASHBOARD_PASSWORD": "",
})

import pytest  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402
from sqlmodel import create_engine  # noqa: E402

from app import db as dbmod  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def fresh_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    dbmod.set_engine(engine)
    dbmod.init_db()
    yield
    dbmod.set_engine(None)
