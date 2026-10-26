import time

from app.willhaben import session as session_mod
from app.willhaben.session import BrowserSession


class _Page:
    def __init__(self, url="https://www.willhaben.at/"):
        self.url, self.visits = url, []

    def is_closed(self):
        return False

    async def goto(self, url, **kw):
        self.visits.append(url)
        self.url = url


def _sess(page, idle_for):
    s = BrowserSession()
    s.ui_page = page
    s._ui_used_at = time.monotonic() - idle_for
    return s


async def test_idle_visible_page_is_parked(monkeypatch):
    monkeypatch.setattr(session_mod, "vnc_viewer_connected", lambda: False)
    page = _Page()
    assert not await _sess(page, 60).park_idle_ui()                     # used a minute ago: stays
    s = _sess(page, session_mod.UI_IDLE_S + 1)
    assert await s.park_idle_ui() and page.visits == ["about:blank"]
    assert not await s.park_idle_ui() and page.visits == ["about:blank"]   # already parked: nothing to do


async def test_page_in_use_or_watched_is_not_parked(monkeypatch):
    page = _Page()
    s = _sess(page, session_mod.UI_IDLE_S + 1)
    monkeypatch.setattr(session_mod, "vnc_viewer_connected", lambda: True)
    assert not await s.park_idle_ui()                                    # someone watches in noVNC
    assert time.monotonic() - s._ui_used_at < 5                          # ... which counts as use
    monkeypatch.setattr(session_mod, "vnc_viewer_connected", lambda: False)
    s._ui_used_at -= session_mod.UI_IDLE_S + 1
    with s.ui_in_use():                                                  # e.g. PayLivery waiting for the bank
        assert not await s.park_idle_ui()
    s._ui_used_at -= session_mod.UI_IDLE_S + 1
    s._capture = {"entries": []}                                         # request recording running
    assert not await s.park_idle_ui()
    assert page.visits == []


def test_vnc_viewer_detection(tmp_path, monkeypatch):
    head = "  sl  local_address rem_address   st\n"
    tcp = tmp_path / "tcp"
    tcp.write_text(head + "   0: 0100007F:170C 00000000:0000 0A\n")              # x11vnc listening only
    real = session_mod.Path
    monkeypatch.setattr(session_mod, "Path", lambda n: real(tcp) if n == "/proc/net/tcp" else real(tmp_path / "none"))
    assert not session_mod.vnc_viewer_connected()
    tcp.write_text(head + "   1: 0100007F:170C 0100007F:D431 01\n")              # websockify connected
    assert session_mod.vnc_viewer_connected()


async def test_the_pages_behind_the_visible_one_are_parked_too(monkeypatch):
    """10-26: the Kleinanzeigen tab was the visible one; the willhaben home behind it and the Vinted tab kept their ads
    (25 renderer processes, 1.2 GB). All shown pages go; the working pages (API, live chat) stay."""
    monkeypatch.setattr(session_mod, "vnc_viewer_connected", lambda: False)
    home, ka, vinted = _Page(), _Page("https://www.kleinanzeigen.de/m-nachrichten.html"), _Page("https://www.vinted.at/inbox")
    api, chat = _Page("https://www.willhaben.at/robots.txt"), _Page("https://www.willhaben.at/robots.txt")
    s = _sess(ka, session_mod.UI_IDLE_S + 1)
    s.home_page, s.site_pages, s.api_page, s.chat_page = home, {"kleinanzeigen": ka, "vinted": vinted}, api, chat
    assert await s.park_idle_ui()
    assert home.visits == ka.visits == vinted.visits == ["about:blank"] and api.visits == chat.visits == []
