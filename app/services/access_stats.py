"""Request statistics per platform and for all sites together: time buckets and totals from the per-minute access log
(app/willhaben/access_log.py).

Storage is UTC epoch minutes; labels are local time (Europe/Vienna, like the rest of the dashboard).
Cache hits (`cached`) are shown separately and never count as an access to the site.
The logged kinds are grouped into a few types for display (TYPES); the rows keep the fine kind.
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
ALL = "all"                                # pseudo platform: every site together

# display types of the logged kinds: what a site's chart stacks by (order = stack order, bottom first); "messages",
# not "chat", so a type never shares its name with the channel "chat" or the kind "chat"
TYPES = ("search", "detail", "messages", "account", "other")
TYPE_OF = {"search": "search", "filters": "search", "detail": "detail", "chat": "messages", "send": "messages",
           "categories": "account", "myads": "account", "publish": "account", "upload": "account", "login": "account",
           "navigation": "account", "other": "other"}


def channel_label(ch: str) -> str:
    return {"anonymous": t("anonymous"), "account": t("account"), "chat": t("live chat")}.get(ch, ch)


def kind_label(k: str) -> str:
    return {
        "search": t("search"), "detail": t("ad detail"), "filters": t("search filters"), "categories": t("categories"),
        "myads": t("my ads"), "chat": t("chat"), "send": t("send message"), "publish": t("publish ad"),
        "upload": t("photo upload"), "login": t("login / profile"), "navigation": t("page navigation"), "other": t("other"),
    }.get(k, k)


SPARK_GROUP = {"1h": 5, "6h": 3, "24h": 4, "7d": 3}   # main buckets per trend bar: 12×5 min, 24×15 min, 24×1 h, 28×6 h
SPARK_W, SPARK_H = 120, 24


def type_label(k: str) -> str:
    return {"search": t("search"), "detail": t("ad detail"), "messages": t("chats & messages"),
            "account": t("my ads & account"), "other": t("other")}.get(k, k)


def site_label(pid: str) -> str:
    from app import platforms

    try:
        return platforms.get(pid).name
    except Exception:  # noqa: BLE001 - "other" (unknown names) or a site that no longer exists
        return t("other") if pid == "other" else pid


def stack_label(stack: str, key: str) -> str:
    return {"channel": channel_label, "type": type_label}[stack](key)


def outcome_label(o: str) -> str:
    return {"ok": t("ok"), "client_error": t("client error (4xx)"), "blocked": t("blocked (403/429)"),
            "error": t("error (5xx/network)"), "cached": t("from cache (no access)")}.get(o, o)


def stacks(platform: str) -> tuple[str, ...]:
    """What the bars of this view can stack by; the first is the default. Channels are willhaben's only; every site
    together stacks by type too (one colour per site does not stay readable), each site is a row with its trend."""
    return ("channel", "type") if platform == "willhaben" else ("type",)


def _rows(first_min: int, last_min: int, bucket_s: int, off: int,
          platform: str | None = "willhaben") -> list[tuple[int, str, str, str, str, int]]:
    """(bucket index, platform, channel, kind, outcome, count) of one platform (None: all) for minutes in
    [first_min, last_min], summed in SQLite, plus the not yet flushed buffer.
    Bucket index = (UTC seconds + local offset) // bucket_s."""
    b = ((AccessCount.minute * 60 + off) // bucket_s).label("b")
    q = (select(b, AccessCount.platform, AccessCount.channel, AccessCount.kind, AccessCount.outcome,
                func.sum(AccessCount.count))
         .where(AccessCount.minute >= first_min, AccessCount.minute <= last_min)
         .group_by(b, AccessCount.platform, AccessCount.channel, AccessCount.kind, AccessCount.outcome))
    if platform is not None:
        q = q.where(AccessCount.platform == platform)
    with session() as db:
        stored = [tuple(r) for r in db.exec(q)]
    pend = [((m * 60 + off) // bucket_s, p, c, k, o, n) for (m, p, c, k, o), n in access_log.pending().items()
            if first_min <= m <= last_min and (platform is None or p == platform)]
    return stored + pend


# live-chat reconnect attempts are not paced by the limiter (backoff up to 60 s, so up to ~60/h in an outage);
# the footer compares against the limiter budget and leaves them out, the stats page shows them
UNPACED = (("chat", "chat", "error"),)


def _not(exclude: tuple[tuple[str, str, str], ...]) -> list[Any]:
    return [~((AccessCount.channel == c) & (AccessCount.kind == k) & (AccessCount.outcome == o)) for c, k, o in exclude]


def last_hour(now: float | None = None, exclude: tuple[tuple[str, str, str], ...] = (),
              platform: str = "willhaben") -> int:
    """Accesses of one platform (cache hits excluded) in the last 60 minutes incl. the current one, from the
    persisted log. `exclude`: (channel, kind, outcome) combinations not to count."""
    now_min = int((now if now is not None else time.time()) // 60)
    first = now_min - 59
    where = [AccessCount.minute >= first, AccessCount.minute <= now_min, AccessCount.outcome != "cached",
             AccessCount.platform == platform, *_not(exclude)]
    with session() as db:
        stored = db.exec(select(func.coalesce(func.sum(AccessCount.count), 0)).where(*where)).one()
    pend = sum(n for (m, p, c, k, o), n in access_log.pending().items()
               if first <= m <= now_min and o != "cached" and (c, k, o) not in exclude and p == platform)
    return int(stored or 0) + pend


def hour_by_platform(now: float | None = None,
                     exclude: tuple[tuple[str, str, str], ...] = UNPACED) -> dict[str, dict[str, int]]:
    """The last 60 minutes per platform in one query: {"n": accesses (cache hits and `exclude` left out, so
    willhaben's n is the footer counter), "blocked": 403/429 answers}."""
    now_min = int((now if now is not None else time.time()) // 60)
    first = now_min - 59
    with session() as db:
        rows = db.exec(select(AccessCount.platform, AccessCount.outcome, func.sum(AccessCount.count))
                       .where(AccessCount.minute >= first, AccessCount.minute <= now_min,
                              AccessCount.outcome != "cached", *_not(exclude))
                       .group_by(AccessCount.platform, AccessCount.outcome)).all()
    out: dict[str, dict[str, int]] = {}

    def add(p: str, o: str, n: int) -> None:
        d = out.setdefault(p, {"n": 0, "blocked": 0})
        d["n"] += n
        if o == "blocked":
            d["blocked"] += n

    for p, o, n in rows:
        add(p, o, int(n or 0))
    for (m, p, c, k, o), n in access_log.pending().items():
        if first <= m <= now_min and o != "cached" and (c, k, o) not in exclude:
            add(p, o, n)
    return out


def by_platform(now: float | None = None) -> dict[str, int]:
    """Accesses in the last hour per platform (cache hits excluded)."""
    return {p: v["n"] for p, v in hour_by_platform(now, exclude=()).items()}


def site_usage(now: float | None = None, hour: dict[str, dict[str, int]] | None = None,
               sites: list[Any] | None = None) -> list[dict[str, Any]]:
    """Every site's last hour against its own budget and limiter, in registry order (`sites`: Platform objects,
    default all known ones). n = accesses without cache hits and unpaced live-chat reconnects (the footer rule).
    tone: full = paused after a block or at 90 % of the budget, warn = at 60 %."""
    from app import platforms

    hour = hour_by_platform(now) if hour is None else hour
    out = []
    for p in platforms.all(enabled_only=False) if sites is None else sites:
        h = hour.get(p.id, {"n": 0, "blocked": 0})
        budget = platform_per_hour(p.id)
        try:
            cool = round(float(p.limiter.cooling_down_s), 1)
        except Exception:  # noqa: BLE001 - a broken adapter must not break the page
            cool = 0.0
        pct = min(100, round(h["n"] * 100 / budget)) if budget else 0
        out.append({"id": p.id, "name": p.name, "enabled": platforms.enabled(p.id), "n": h["n"],
                    "blocked": h["blocked"], "budget": budget, "pct": pct, "cooling_down_s": cool,
                    "tone": "full" if cool or (budget and pct >= 90) else "warn" if budget and pct >= 60 else "ok"})
    return out


def _local(ts: float, fmt: str) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).astimezone(VIENNA).strftime(fmt)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def series(rng: str = DEFAULT_RANGE, now: float | None = None, platform: str = "willhaben",
           by: str | None = None) -> dict[str, Any]:
    """Buckets (oldest first, the last one is the current, partial bucket) plus totals of one platform or of ALL.
    The bars stack by `by` (stacks(platform); the first by default): channel or type. ALL adds one row per site
    with its totals, its last hour against its budget and its trend (SPARK_GROUP buckets per point)."""
    rng = rng if rng in RANGES else DEFAULT_RANGE
    every = platform == ALL
    choices = stacks(platform)
    stack = by if by in choices else choices[0]
    span, bucket_s = RANGES[rng]
    now = time.time() if now is None else now
    n = span // bucket_s
    group = SPARK_GROUP[rng]
    n_spark = n // group
    # buckets aligned to local time (2-h bars of 7d start at 00:00, 02:00 ... Vienna time, also across DST)
    off = int(datetime.fromtimestamp(now, VIENNA).utcoffset().total_seconds())
    first_idx = int((now + off) // bucket_s) - n + 1
    first_ts = first_idx * bucket_s - off
    fmt = "%d.%m. %H:%M" if rng == "7d" else "%H:%M"
    buckets = [{"start": _iso(first_ts + i * bucket_s), "ts": first_ts + i * bucket_s,
                "label": _local(first_ts + i * bucket_s, fmt), "end_label": _local(first_ts + (i + 1) * bucket_s, "%H:%M"),
                "total": 0, "blocked": 0, "cached": 0, **{c: 0 for c in CHANNELS},
                "by_type": dict.fromkeys(TYPES, 0), "blocked_by": {}, **({"by_site": {}} if every else {})}
               for i in range(n)]
    by_channel = {c: 0 for c in CHANNELS}
    by_kind = {k: 0 for k in KINDS}
    by_outcome = {o: 0 for o in OUTCOMES}
    by_type = dict.fromkeys(TYPES, 0)
    type_outcomes: dict[str, dict[str, int]] = {}
    kinds_of: dict[str, dict[str, int]] = {}
    sites: dict[str, dict[str, Any]] = {}
    for bidx, pid, ch, kind, outcome, cnt in _rows(first_ts // 60, int(now // 60), bucket_s, off,
                                                   None if every else platform):
        idx = bidx - first_idx
        if not 0 <= idx < n:
            continue
        b = buckets[idx]
        typ = TYPE_OF.get(kind, "other")
        by_outcome[outcome] = by_outcome.get(outcome, 0) + cnt
        to = type_outcomes.setdefault(typ, dict.fromkeys(OUTCOMES, 0))
        to[outcome] = to.get(outcome, 0) + cnt
        s = sites.setdefault(pid, {"total": 0, "blocked": 0, "cached": 0, "errors": 0, "by_type": dict.fromkeys(TYPES, 0),
                                   "spark": [[0, 0] for _ in range(n_spark)]}) if every else None
        if outcome == "cached":
            b["cached"] += cnt
            if s is not None:
                s["cached"] += cnt
            continue
        b[ch] = b.get(ch, 0) + cnt
        b["by_type"][typ] += cnt
        b["total"] += cnt
        by_channel[ch] = by_channel.get(ch, 0) + cnt
        by_kind[kind] = by_kind.get(kind, 0) + cnt
        by_type[typ] += cnt
        kinds_of.setdefault(typ, {})
        kinds_of[typ][kind] = kinds_of[typ].get(kind, 0) + cnt
        if s is not None:
            b["by_site"][pid] = b["by_site"].get(pid, 0) + cnt
            s["total"] += cnt
            s["by_type"][typ] += cnt
            s["spark"][min(idx // group, n_spark - 1)][0] += cnt
            s["errors"] += cnt if outcome in ("client_error", "error") else 0
        if outcome == "blocked":
            b["blocked"] += cnt
            key = typ if stack == "type" else ch
            b["blocked_by"][key] = b["blocked_by"].get(key, 0) + cnt
            if s is not None:
                s["blocked"] += cnt
                s["spark"][min(idx // group, n_spark - 1)][1] += cnt
    for o in type_outcomes.values():
        o["errors"] = o["client_error"] + o["error"]
        o["requests"] = o["ok"] + o["blocked"] + o["errors"]
    total = sum(by_channel.values())
    peak = max(buckets, key=lambda b: b["total"])
    out: dict[str, Any] = {
        "platform": platform, "stack": stack, "stacks": list(choices),
        "range": rng, "ranges": list(RANGES), "bucket_s": bucket_s, "from": _iso(first_ts), "to": _iso(now),
        "channels": list(CHANNELS), "types": list(TYPES), "buckets": buckets, "total": total,
        "cached": by_outcome.get("cached", 0), "blocked": by_outcome.get("blocked", 0),
        "by_channel": by_channel, "by_kind": {k: v for k, v in sorted(by_kind.items(), key=lambda kv: -kv[1]) if v},
        "by_type": by_type, "type_outcomes": {k: type_outcomes[k] for k in TYPES if k in type_outcomes},
        "type_kinds": {ty: dict(sorted(kinds_of[ty].items(), key=lambda kv: -kv[1])) for ty in TYPES
                       if ty in kinds_of and (len(kinds_of[ty]) > 1 or next(iter(kinds_of[ty])) != ty)},
        "by_outcome": by_outcome,
        "peak": {"start": peak["start"], "label": peak["label"], "total": peak["total"]} if peak["total"] else None,
    }
    if every:
        hour = hour_by_platform(now)
        usage = {u["id"]: u for u in site_usage(now, hour)}
        order = list(usage) + sorted(p for p in sites if p not in usage)
        rows = []
        for pid in order:
            u = usage.get(pid)
            if pid not in sites and not (u and u["enabled"]):
                continue                           # switched off and silent in this range
            s = sites.get(pid) or {"total": 0, "blocked": 0, "cached": 0, "errors": 0, "by_type": dict.fromkeys(TYPES, 0),
                                   "spark": [[0, 0] for _ in range(n_spark)]}
            rows.append({"id": pid, "known": u is not None, "enabled": bool(u and u["enabled"]),
                         **{k: v for k, v in s.items() if k != "spark"},
                         "last_hour": hour.get(pid, {}).get("n", 0), "budget": u["budget"] if u else 0,
                         "pct": u["pct"] if u else 0, "tone": u["tone"] if u else "ok",
                         "cooling_down_s": u["cooling_down_s"] if u else 0.0,
                         "spark": [{"start": buckets[i * group]["start"], "total": tot, "blocked": blk}
                                   for i, (tot, blk) in enumerate(s["spark"])]})
        out.update({
            "sites": rows, "by_site": {r["id"]: r["total"] for r in rows},
            "type_cols": [k for k in TYPES if by_type[k]], "spark_bucket_s": bucket_s * group,
            "budget": None, "budget_per_bucket": None, "last_hour": sum(v["n"] for v in hour.values()),
        })
    else:
        budget = platform_per_hour(platform)
        out.update({
            "budget": budget,
            "budget_per_bucket": round(budget * bucket_s / 3600, 2) if budget and rng in BUDGET_LINE_RANGES else None,
            "last_hour": last_hour(now, exclude=UNPACED, platform=platform),
        })
    out["stack_keys"] = list(CHANNELS) if stack == "channel" else [k for k in TYPES if by_type[k]]
    return out


def public_json(data: dict[str, Any], legacy: bool = False) -> dict[str, Any]:
    """Shape of /api/stats/requests (no display labels). legacy: /api/stats/willhaben keeps its bucket keys
    (start, total, blocked, cached and the three channels), which Home Assistant templates may read."""
    drop = {"ts", "label", "end_label"} | ({"by_type", "by_site", "blocked_by"} if legacy else set())
    out = {k: v for k, v in data.items() if k != "buckets"}
    out["buckets"] = [{k: v for k, v in b.items() if k not in drop} for b in data["buckets"]]
    return out


# ------------------------------------------------------------------ inline SVG chart geometry
W, H = 720, 240
PAD_L, PAD_R, PAD_T, PAD_B = 40, 8, 10, 26
SEG_GAP = 1.5          # strip of card surface between stacked segments (viewBox units, ~2 px on screen)


def _nice_step(top: float, ticks: int = 4) -> float:
    raw = max(top, 1) / ticks
    mag = 10 ** len(str(int(raw))) / 10 if raw >= 1 else 1
    for m in (1, 2, 2.5, 5, 10):
        if m * mag >= raw:
            return max(1.0, m * mag)
    return 10 * mag


def stack_parts(data: dict[str, Any], b: dict[str, Any]) -> dict[str, int]:
    """One bucket's counts per stack key (channel counts sit flat in the bucket, types nested)."""
    src = b if data["stack"] == "channel" else b["by_type"]
    return {k: src.get(k, 0) for k in data["stack_keys"]}


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
    stack = data["stack"]

    def y(v: float) -> float:
        return round(PAD_T + plot_h - v / y_max * plot_h, 2)

    bars = []
    for i, b in enumerate(buckets):
        x = round(PAD_L + i * slot + gap / 2, 2)
        parts = stack_parts(data, b)
        segs, acc = [], 0
        for k, v in parts.items():
            if v:
                h = v / y_max * plot_h
                segs.append({"key": k, "y": y(acc + v), "h": round(max(h - SEG_GAP, 0.75) if acc else h, 2)})
                acc += v
        listed = ", ".join(f"{stack_label(stack, k)} {v}" for k, v in parts.items() if v) or "–"
        if data["platform"] == ALL:
            tip = t("{start}–{end}: {n} requests ({parts})", start=b["label"], end=b["end_label"], n=b["total"],
                    parts=listed)
            per_site = ", ".join(f"{site_label(r['id'])} {b['by_site'][r['id']]}" for r in data["sites"]
                                 if b["by_site"].get(r["id"]))
            tip += f"; {per_site}" if per_site else ""
        else:
            tip = t("{start}–{end}: {n} {platform} requests ({parts})", start=b["label"], end=b["end_label"],
                    n=b["total"], platform=site_label(data["platform"]), parts=listed)
        if b["blocked"]:
            tip += "; " + t("{n} blocked ({parts})", n=b["blocked"], parts=", ".join(
                f"{stack_label(stack, k)} {v}" for k, v in b["blocked_by"].items()))
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
    sparks = {r["id"]: sp for r in data.get("sites") or [] if (sp := _spark(r, data))} if data["platform"] == ALL else {}
    return {"w": W, "h": H, "bars": bars, "yticks": yticks, "xlabels": xlabels, "x0": PAD_L, "x1": W - PAD_R,
            "base_y": PAD_T + plot_h, "budget_y": y(line) if line else None, "budget": f"{line:g}" if line else None,
            "sparks": sparks}


def _spark(row: dict[str, Any], data: dict[str, Any]) -> dict[str, Any] | None:
    """A site's trend for the all-sites table: one bar per SPARK_GROUP buckets, blocked share on top, and the
    site's budget pace as a line; None when the site was silent."""
    points = row["spark"]
    if not any(p["total"] for p in points):
        return None
    group = SPARK_GROUP[data["range"]]
    pace = row["budget"] * data["spark_bucket_s"] / 3600 if row["budget"] and data["range"] in BUDGET_LINE_RANGES else None
    top = max([p["total"] for p in points] + [pace or 0, 1])
    slot = SPARK_W / len(points)

    def y(v: float) -> float:
        return round(SPARK_H - v / top * (SPARK_H - 1), 2)

    bars = []
    for i, p in enumerate(points):
        if not p["total"]:
            continue
        h = max(SPARK_H - y(p["total"]), 1)
        first, last = data["buckets"][i * group], data["buckets"][min(i * group + group - 1, len(data["buckets"]) - 1)]
        title = t("{start}–{end}: {n} {platform} requests", start=first["label"], end=last["end_label"], n=p["total"],
                  platform=site_label(row["id"]))
        if p["blocked"]:
            title += "; " + t("{n} blocked", n=p["blocked"])
        bars.append({"x": round(i * slot, 2), "y": round(SPARK_H - h, 2), "w": round(max(slot - 1, 1), 2),
                     "h": round(h, 2), "bh": round(max(h * p["blocked"] / p["total"], 1), 2) if p["blocked"] else 0,
                     "title": title})
    return {"w": SPARK_W, "h": SPARK_H, "bars": bars, "budget_y": y(pace) if pace else None}


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
