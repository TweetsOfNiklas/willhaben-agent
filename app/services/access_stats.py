"""Request statistics per platform: time buckets and totals from the per-minute access log (app/willhaben/access_log.py).

Storage is UTC epoch minutes; labels are local time (Europe/Vienna, like the rest of the dashboard).
Cache hits (`cached`) are shown separately and never count as a willhaben access.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlmodel import func, select

from app.db import AccessCount, platform_per_hour, session
from app.i18n import t
from app.willhaben import access_log
from app.willhaben.access_log import CHANNELS, KINDS, OUTCOMES

VIENNA = ZoneInfo("Europe/Vienna")
# range -> (span seconds, bucket seconds)
RANGES: dict[str, tuple[int, int]] = {"1h": (3600, 60), "6h": (6 * 3600, 300), "24h": (86400, 900), "7d": (7 * 86400, 7200)}
DEFAULT_RANGE = "24h"
BUDGET_LINE_RANGES = ("1h", "6h", "24h")   # 2-h buckets of 7d would flatten every bar under the line


def channel_label(ch: str) -> str:
    return {"anonymous": t("anonymous"), "account": t("account"), "chat": t("live chat")}.get(ch, ch)


def kind_label(k: str) -> str:
    return {
        "search": t("search"), "detail": t("ad detail"), "filters": t("search filters"), "categories": t("categories"),
        "myads": t("my ads"), "chat": t("chat"), "send": t("send message"), "publish": t("publish ad"),
        "upload": t("photo upload"), "login": t("login / profile"), "navigation": t("page navigation"), "other": t("other"),
    }.get(k, k)


def outcome_label(o: str) -> str:
    return {"ok": t("ok"), "client_error": t("client error (4xx)"), "blocked": t("blocked (403/429)"),
            "error": t("error (5xx/network)"), "cached": t("from cache (no access)")}.get(o, o)


def _rows(first_min: int, last_min: int, bucket_s: int, off: int,
          platform: str = "willhaben") -> list[tuple[int, str, str, str, int]]:
    """(bucket index, channel, kind, outcome, count) of one platform for minutes in [first_min, last_min], summed in
    SQLite, plus the not yet flushed buffer. Bucket index = (UTC seconds + local offset) // bucket_s."""
    b = ((AccessCount.minute * 60 + off) // bucket_s).label("b")
    with session() as db:
        stored = [tuple(r) for r in db.exec(
            select(b, AccessCount.channel, AccessCount.kind, AccessCount.outcome, func.sum(AccessCount.count))
            .where(AccessCount.minute >= first_min, AccessCount.minute <= last_min, AccessCount.platform == platform)
            .group_by(b, AccessCount.channel, AccessCount.kind, AccessCount.outcome))]
    pend = [((m * 60 + off) // bucket_s, c, k, o, n) for (m, p, c, k, o), n in access_log.pending().items()
            if first_min <= m <= last_min and p == platform]
    return stored + pend


# live-chat reconnect attempts are not paced by the limiter (backoff up to 60 s, so up to ~60/h in an outage);
# the footer compares against the limiter budget and leaves them out, the stats page shows them
UNPACED = (("chat", "chat", "error"),)


def last_hour(now: float | None = None, exclude: tuple[tuple[str, str, str], ...] = (),
              platform: str = "willhaben") -> int:
    """Accesses of one platform (cache hits excluded) in the last 60 minutes incl. the current one, from the
    persisted log. `exclude`: (channel, kind, outcome) combinations not to count."""
    now_min = int((now if now is not None else time.time()) // 60)
    first = now_min - 59
    where = [AccessCount.minute >= first, AccessCount.minute <= now_min, AccessCount.outcome != "cached",
             AccessCount.platform == platform]
    for c, k, o in exclude:
        where.append(~((AccessCount.channel == c) & (AccessCount.kind == k) & (AccessCount.outcome == o)))
    with session() as db:
        stored = db.exec(select(func.coalesce(func.sum(AccessCount.count), 0)).where(*where)).one()
    pend = sum(n for (m, p, c, k, o), n in access_log.pending().items()
               if first <= m <= now_min and o != "cached" and (c, k, o) not in exclude and p == platform)
    return int(stored or 0) + pend


def by_platform(now: float | None = None) -> dict[str, int]:
    """Accesses in the last hour per platform (cache hits excluded)."""
    now_min = int((now if now is not None else time.time()) // 60)
    first = now_min - 59
    with session() as db:
        rows = db.exec(select(AccessCount.platform, func.sum(AccessCount.count))
                       .where(AccessCount.minute >= first, AccessCount.minute <= now_min, AccessCount.outcome != "cached")
                       .group_by(AccessCount.platform)).all()
    out = {p: int(n or 0) for p, n in rows}
    for (m, p, _c, _k, o), n in access_log.pending().items():
        if first <= m <= now_min and o != "cached":
            out[p] = out.get(p, 0) + n
    return out


def _local(ts: float, fmt: str) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).astimezone(VIENNA).strftime(fmt)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def series(rng: str = DEFAULT_RANGE, now: float | None = None, platform: str = "willhaben") -> dict[str, Any]:
    """Buckets of one platform (oldest first, the last one is the current, partial bucket) stacked by channel, plus
    totals."""
    rng = rng if rng in RANGES else DEFAULT_RANGE
    span, bucket_s = RANGES[rng]
    now = time.time() if now is None else now
    n = span // bucket_s
    # buckets aligned to local time (2-h bars of 7d start at 00:00, 02:00 ... Vienna time, also across DST)
    off = int(datetime.fromtimestamp(now, VIENNA).utcoffset().total_seconds())
    first_idx = int((now + off) // bucket_s) - n + 1
    first_ts = first_idx * bucket_s - off
    fmt = "%d.%m. %H:%M" if rng == "7d" else "%H:%M"
    buckets = [{"start": _iso(first_ts + i * bucket_s), "ts": first_ts + i * bucket_s,
                "label": _local(first_ts + i * bucket_s, fmt), "end_label": _local(first_ts + (i + 1) * bucket_s, "%H:%M"),
                "total": 0, "blocked": 0, "cached": 0, **{c: 0 for c in CHANNELS}} for i in range(n)]
    by_channel = {c: 0 for c in CHANNELS}
    by_kind = {k: 0 for k in KINDS}
    by_outcome = {o: 0 for o in OUTCOMES}
    for bidx, ch, kind, outcome, cnt in _rows(first_ts // 60, int(now // 60), bucket_s, off, platform):
        idx = bidx - first_idx
        if not 0 <= idx < n:
            continue
        b = buckets[idx]
        by_outcome[outcome] = by_outcome.get(outcome, 0) + cnt
        if outcome == "cached":
            b["cached"] += cnt
            continue
        b[ch] = b.get(ch, 0) + cnt
        b["total"] += cnt
        by_channel[ch] = by_channel.get(ch, 0) + cnt
        by_kind[kind] = by_kind.get(kind, 0) + cnt
        if outcome == "blocked":
            b["blocked"] += cnt
    total = sum(by_channel.values())
    peak = max(buckets, key=lambda b: b["total"])
    budget = platform_per_hour(platform)
    return {
        "platform": platform,
        "range": rng, "ranges": list(RANGES), "bucket_s": bucket_s, "from": _iso(first_ts), "to": _iso(now),
        "channels": list(CHANNELS), "buckets": buckets, "total": total,
        "cached": by_outcome.get("cached", 0), "blocked": by_outcome.get("blocked", 0),
        "by_channel": by_channel, "by_kind": {k: v for k, v in sorted(by_kind.items(), key=lambda kv: -kv[1]) if v},
        "by_outcome": by_outcome,
        "peak": {"start": peak["start"], "label": peak["label"], "total": peak["total"]} if peak["total"] else None,
        "budget": budget,
        "budget_per_bucket": round(budget * bucket_s / 3600, 2) if budget and rng in BUDGET_LINE_RANGES else None,
        "last_hour": last_hour(now, platform=platform),
    }


def public_json(data: dict[str, Any]) -> dict[str, Any]:
    """Shape of /api/stats/willhaben (no display labels)."""
    out = {k: v for k, v in data.items() if k != "buckets"}
    out["buckets"] = [{k: v for k, v in b.items() if k not in ("ts", "label", "end_label")} for b in data["buckets"]]
    return out


# ------------------------------------------------------------------ inline SVG chart geometry
W, H = 720, 240
PAD_L, PAD_R, PAD_T, PAD_B = 40, 8, 10, 26


def _nice_step(top: float, ticks: int = 4) -> float:
    raw = max(top, 1) / ticks
    mag = 10 ** len(str(int(raw))) / 10 if raw >= 1 else 1
    for m in (1, 2, 2.5, 5, 10):
        if m * mag >= raw:
            return max(1.0, m * mag)
    return 10 * mag


def chart(data: dict[str, Any]) -> dict[str, Any]:
    """Coordinates for the stacked bar chart in stats_willhaben.html (viewBox W x H)."""
    buckets = data["buckets"]
    n = len(buckets)
    line = data.get("budget_per_bucket")
    top = max([b["total"] for b in buckets] + [line or 0, 1])
    step = _nice_step(top)
    y_max = step * -(-top // step)
    plot_w, plot_h = W - PAD_L - PAD_R, H - PAD_T - PAD_B
    slot = plot_w / n
    gap = 2 if slot >= 6 else 1
    bw = max(slot - gap, 1)

    def y(v: float) -> float:
        return round(PAD_T + plot_h - v / y_max * plot_h, 2)

    bars = []
    for i, b in enumerate(buckets):
        x = round(PAD_L + i * slot + gap / 2, 2)
        segs, acc = [], 0
        for ch in data["channels"]:
            v = b.get(ch, 0)
            if v:
                segs.append({"channel": ch, "y": y(acc + v), "h": round(v / y_max * plot_h, 2)})
                acc += v
        parts = ", ".join(f"{channel_label(ch)} {b.get(ch, 0)}" for ch in data["channels"])
        tip = t("{start}–{end}: {n} willhaben requests ({parts})", start=b["label"], end=b["end_label"], n=b["total"],
                parts=parts)
        if b["blocked"]:
            tip += "; " + t("{n} blocked", n=b["blocked"])
        if b["cached"]:
            tip += "; " + t("{n} from cache", n=b["cached"])
        bars.append({"x": x, "w": round(bw, 2), "segs": segs, "title": tip, "blocked": b["blocked"],
                     "hit_y": PAD_T, "hit_h": plot_h})
    yticks = []
    v = 0.0
    while v <= y_max + 1e-9:
        yticks.append({"y": y(v), "label": f"{v:g}"})
        v += step
    every = max(1, round(n / 6))
    xlabels = [{"x": round(PAD_L + i * slot + slot / 2, 2), "label": b["label"]}
               for i, b in enumerate(buckets) if i % every == 0]
    return {"w": W, "h": H, "bars": bars, "yticks": yticks, "xlabels": xlabels, "x0": PAD_L, "x1": W - PAD_R,
            "base_y": PAD_T + plot_h, "budget_y": y(line) if line else None, "budget": f"{line:g}" if line else None}


CHANNEL_COLORS = {"anonymous": "#2a78d6", "account": "#eb6834", "chat": "#1baf7a"}


def popup(now: float | None = None) -> dict[str, Any]:
    """The willhaben usage popover in the sidebar: the last hour per channel and the last 6 hours in 15-min bars."""
    hour = series("1h", now)
    six = series("6h", now)                           # 5-min buckets, merged into 15-min bars
    bars = []
    for i in range(0, len(six["buckets"]), 3):
        group = six["buckets"][i:i + 3]
        bar = {c: sum(b.get(c, 0) for b in group) for c in six["channels"]}
        bar["total"] = sum(bar.values())
        bar["title"] = f"{group[0]['label']}–{group[-1]['end_label']}: " + ", ".join(
            f"{channel_label(c)} {bar[c]}" for c in six["channels"] if bar[c]) if bar["total"] else f"{group[0]['label']}: 0"
        bars.append(bar)
    top = max([b["total"] for b in bars] + [1])
    for b in bars:
        b["h"] = round(b["total"] * 100 / top)
        b["parts"] = [(c, round(b[c] * 100 / b["total"]) if b["total"] else 0) for c in six["channels"]]
    return {"channels": [{"key": c, "label": channel_label(c), "n": hour["by_channel"].get(c, 0),
                          "color": CHANNEL_COLORS.get(c, "var(--muted)")} for c in hour["channels"]],
            "total": hour["total"], "bars": bars}
