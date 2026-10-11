"""Runtime configuration from environment / .env (secrets and infrastructure only).

User-editable preferences (reply rules, toggles) live in the database `setting` table.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    data_dir: Path = Path("./data")

    # --- Claude (Option B: `claude -p` with the Max subscription token) ---
    llm_backend: str = "claude_cli"
    claude_bin: str = "claude"
    llm_model: str = "opus"
    llm_timeout_s: int = 240
    # Read by the claude CLI itself from the environment; listed here so it is documented and checked.
    claude_code_oauth_token: str | None = None

    # --- willhaben access ---
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    )
    request_min_gap_s: float = 3.0       # between any two willhaben requests (search, pages, account)
    requests_per_hour: int = 240         # hard budget over all willhaben requests; 0 = off
    default_agent_interval_s: int = 300
    min_agent_interval_s: int = 120
    search_rows: int = 30

    # --- logged-in browser session (Playwright persistent profile) ---
    browser_enabled: bool = True
    browser_headless: bool = False
    novnc_url: str | None = None  # e.g. http://homeserver:6080/vnc.html?autoconnect=1&resize=scale
    chat_poll_interval_s: int = 300
    humanize_chat: bool = True   # human-like delays + read receipts for auto replies (settings can switch off)
    session_check_interval_s: int = 900

    # --- my ads ---
    myads_check_hour: int = 8

    # --- Home Assistant notifications ---
    ha_url: str | None = None
    ha_token: str | None = None
    ha_notify_service: str | None = None  # e.g. "mobile_app_pixel_8" -> notify.mobile_app_pixel_8
    ha_listener: bool = True  # keep a websocket to HA for push-button taps (off in tests)
    telegram_bot_token: str = ""  # from @BotFather; Settings stores one in DATA_DIR/telegram-token (wins)
    telegram_listener: bool = True  # long-poll Telegram for button taps (off in tests)
    chat_live: bool = True    # keep the willhaben chat WebSocket open instead of relying on polling

    # --- web app ---
    public_url: str = "http://localhost:8000"
    # Optional HTTP Basic auth for dashboard + API (recommended: the app can send messages for you).
    dashboard_user: str = "admin"
    dashboard_password: str | None = None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def profile_dir(self) -> Path:
        return self.data_dir / "browser-profile"

    @property
    def captures_dir(self) -> Path:
        return self.data_dir / "captures"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "ad-drafts"


def address_check(url: str | None) -> dict[str, bool]:
    """Can a phone open links to this dashboard address? `reachable`: not a loopback/placeholder host;
    `https`: Telegram buttons need it."""
    from urllib.parse import urlsplit

    parts = urlsplit((url or "").strip())
    host = (parts.hostname or "").lower()
    reachable = parts.scheme in ("http", "https") and host not in ("", "localhost", "127.0.0.1", "0.0.0.0", "::1")
    return {"reachable": reachable, "https": reachable and parts.scheme == "https"}


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    for d in (s.captures_dir, s.uploads_dir):
        d.mkdir(parents=True, exist_ok=True)
    return s
