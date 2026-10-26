"""How far to trust the other person by their account: its age and the ratings it received, where the site shows
them (willhaben: the sign-up date on every ad, PayLivery ratings on the public profile; Vinted: the seller's
ratings; eBay: "member since" and the rating count). Warnings only – nothing is decided on them."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.i18n import t

YOUNG_DAYS = 90          # an account younger than this is worth a second look
VERY_YOUNG_DAYS = 30     # … and younger than this a warning in red


def _days(since: datetime, now: datetime) -> int:
    since = since if since.tzinfo else since.replace(tzinfo=timezone.utc)      # SQLite: naive UTC
    return max(0, (now - since).days)


def flags(since: datetime | None, ratings: int | None, platform: str | None = "willhaben",
          now: datetime | None = None) -> list[dict[str, Any]]:
    """[{code, label, severity}] for a young account ("high" under VERY_YOUNG_DAYS, else "medium") and one without a
    single rating ("medium"); [] when neither is known to apply."""
    now = now or datetime.now(timezone.utc)
    out = []
    if since is not None and (days := _days(since, now)) < YOUNG_DAYS:
        out.append({"code": "young", "severity": "high" if days < VERY_YOUNG_DAYS else "medium",
                    "label": t("Account only {n} days old", n=days) if days != 1 else t("Account only 1 day old")})
    if ratings == 0:
        out.append({"code": "no_ratings", "severity": "medium",
                    "label": t("No PayLivery ratings yet") if (platform or "willhaben") == "willhaben"
                    else t("No ratings yet")})
    return out


def facts(since: datetime | None, ratings: int | None, reply_time: str | None = None) -> list[str]:
    """The neutral facts behind flags(), for a line under the name: "on willhaben since 2012", "9 ratings"."""
    out = []
    if since is not None:
        out.append(t("member since {year}", year=since.year))
    if ratings:
        out.append(t("1 rating") if ratings == 1 else t("{n} ratings", n=ratings))
    if reply_time:
        out.append(reply_time)
    return out
