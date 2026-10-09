"""Cheap scam check for search-agent hits: rules on price and text, no LLM.

`flags(item, context)` returns [{code, label, severity, params}] sorted most severe first. Severity:
- "high": classic scam pattern (Western Union, brand-new luxury item at a fraction of the price, far too cheap)
- "warn": worth a second look (prepayment only, contact only outside willhaben, foreign IBAN/phone, ...)
- "info": shown only in the ad viewer, never in pushes (phone number in the text, dealer/private mismatch)

False positives are the main risk, so every rule needs a concrete signal and price rules skip parts, defects,
wanted ads and anything below 10 % of the median (in a search that is nearly always an accessory, not a scam).
The thresholds are a first guess from typical scam ads; they have not been tuned on real willhaben data.
"""
from __future__ import annotations

import re
import statistics
from datetime import timedelta
from typing import Any

from app.i18n import t

MIN_SAMPLES = 8          # agent hits with a price needed before the median counts
HIGH_RATIO = 0.35        # price below 35 % of the median -> high
WARN_RATIO = 0.50        # below 50 % -> warn
FLOOR_RATIO = 0.10       # below 10 %: almost always an accessory/part in the search, not a scam -> no price flag
MAX_SPREAD = 3.0         # p75/p25 above this: the search mixes very different items, its median says nothing
CONTEXT_DAYS = 60
RANK = {"info": 0, "warn": 1, "high": 2}

_I = re.I | re.U
# cheap for a legitimate reason: parts, defects, wanted/swap/rent ads, giveaways
CHEAP_OK = re.compile(r"\b(defekt\w*|kaputt|bastler\w*|ersatzteil\w*|teile|für teile|zu verschenken|verschenke|gratis|"
                      r"tausch\w*|suche|gesucht|miete[n]?|vermiete\w*|verleih\w*|leihen|reparatur\w*|"
                      r"hülle|case|cover|attrappe|dummy|leerkarton|karton|ovp leer|nur (die )?(box|verpackung))\b", _I)
PREPAY_ONLY = re.compile(r"\b(nur|ausschlie(?:ß|ss)lich)\s+(?:(?:gegen|per|mit|bei|über)\s+)?"
                         r"(vor(?:aus)?kasse|vorab[- ]?(?:überweisung|zahlung)|überweisung)\b", _I)
PICKUP_OK = re.compile(r"\b(abholung|abholen|selbstabhol\w*|barzahlung|bar bei|paylivery|treffen|persönlich)\b", _I)
NO_PICKUP = re.compile(r"\bkeine (selbst)?abholung\b|\babholung (ist )?(leider )?nicht möglich\b", _I)
MONEY_TRANSFER = re.compile(r"\b(western\s?union|money\s?gram|ria money|paysafe\s?(card|karte)?|"
                            r"(amazon|itunes|google[- ]play|steam)[- ]?(gutschein|karte|guthaben))", _I)
CRYPTO_PAY = re.compile(r"\b(zahlung|bezahlung|bezahlen|zahle|überweis\w*)\b[^.\n]{0,30}\b(bitcoin|btc|krypto\w*|usdt|ethereum)\b", _I)
COURIER = re.compile(r"\b(kurier\w*|spedition\w*|transport(unternehmen|firma|dienst)|lieferdienst|lieferfirma)\b", _I)
ABROAD = re.compile(r"\b(im ausland|aus dem ausland|ins ausland|beruflich (im ausland|unterwegs)|auf montage|"
                    r"nicht (mehr )?in österreich|derzeit in|zurzeit in|momentan in)\b", _I)
SHIP_ONLY = re.compile(r"\b(nur|ausschlie(?:ß|ss)lich)\s+(per\s+)?(post)?versand\b|" + NO_PICKUP.pattern, _I)
OFF_PLATFORM = re.compile(r"\b(nur|ausschlie(?:ß|ss)lich)\s+(?:(?:über|per|via|auf|an|mit|unter)\s+)?"
                          r"(whats\s?app|telegram|signal|viber|e-?mail|mail)\b|"
                          r"\bkeine willhaben[- ]?nachrichten\b|\bantworte nicht (auf|über) willhaben\b|"
                          r"\b(willhaben[- ]?)?nachrichten (hier )?(werden )?nicht gelesen\b", _I)
MESSENGER = re.compile(r"\b(whats\s?app|telegram|viber)\b", _I)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]*[a-z]{2,}", _I)
EMAIL_SPELLED = re.compile(r"\b[\w.+-]+\s*(\(at\)|\[at\])\s*[\w-]+\s*(\.|\(punkt\)|\(dot\))\s*[a-z]{2,}\b", _I)
PHONE_INTL = re.compile(r"(?<![\w+])(?:\+|00)[1-9][\d \t/().-]{7,20}\d")
PHONE_NAT = re.compile(r"(?<![\w.,/])0[1-9]\d{1,4}(?:[ /-]?\d{2,}){1,4}(?!\w|[.,]\d)")
IBAN = re.compile(r"\b([A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?)\b")
NEW_WORD = re.compile(r"\b(neu|neuware|brandneu|nagelneu|ovp|originalverpackt|versiegelt|sealed|unbenutzt|"
                      r"ungeöffnet|ungeoeffnet|ungetragen)\b", _I)
LUXURY = re.compile(r"\b(rolex|patek philippe|audemars piguet|cartier|omega (seamaster|speedmaster|constellation)|"
                    r"louis vuitton|gucci|chanel|herm[eè]s|prada|bottega veneta|"
                    r"iphone 1[3-9]( pro)?( max)?|macbook (air|pro)|ipad pro|apple watch|airpods( pro| max)?|"
                    r"playstation 5|ps5|xbox series x|nintendo switch 2|dyson|rtx ?[45]0[789]0)\b", _I)
NOT_THE_ITEM = re.compile(r"\b(hülle|case|cover|schutz\w*|folie|panzerglas|glas|kabel|ladegerät|netzteil|adapter|"
                          r"armband|strap|box|karton|verpackung|dust ?bag|staubbeutel|attrappe|dummy|repli\w*|imitat|"
                          r"fake|nachbau|controller|spiel|game|halterung|ständer|display|akku|ersatz\w*|parf[uü]m|"
                          r"eau de|duft|sonnenbrille|gutschein|für (iphone|ipad|macbook|ps5|playstation|apple watch))\b", _I)
# brand-new at less than this without any agent statistics is not plausible (only very clear cases)
LUXURY_FLOOR = [(re.compile(r"\brolex\b", _I), 1500), (re.compile(r"\b(patek philippe|audemars piguet)\b", _I), 3000),
                (re.compile(r"\biphone 1[6-9] pro\b", _I), 300), (re.compile(r"\b(playstation 5|ps5)\b", _I), 150)]
DEALER_TEXT = re.compile(r"\b(\d+ (monate|jahre?) gewährleistung|gewährleistung von \d+|inkl\.? (mwst|ust)|zzgl\.? (mwst|ust)|rechnung mit (mwst|ust)|"
                         r"uid[- ]?nr|firmenbuch|unser (shop|geschäft|sortiment|lager)|öffnungszeiten|filiale|"
                         r"lagerware|mehrere stück (verfügbar|lagernd))\b", _I)
PRIVATE_TEXT = re.compile(r"\b(privatverkauf|privat ?verkauf|keine garantie|keine gewährleistung|keine rücknahme)\b", _I)
# neighbours and common EU countries: a German or Swiss number is normal for a seller near the border
NEAR_CC = ("43", "49", "41", "423", "420", "421", "36", "386", "39")


def _label(code: str, **p: Any) -> str:
    """One short label per flag. Kept as code + params so the UI can re-render it in the viewer's language."""
    labels = {
        "price_far_below": lambda: t("Price only {pct} % of the usual {median} €", **p),
        "price_below": lambda: t("Price {pct} % of the usual {median} €", **p),
        "luxury_new_cheap": lambda: t("Brand-new luxury item at a fraction of the price"),
        "money_transfer": lambda: t("Payment via {what}", **p),
        "crypto": lambda: t("Payment in crypto"),
        "prepay_only": lambda: t("Prepayment only, no pickup"),
        "courier_abroad": lambda: t("Seller abroad, delivery by courier"),
        "courier_cheap": lambda: t("Courier delivery at a very low price"),
        "ship_only_cheap": lambda: t("Shipping only, at a very low price"),
        "off_platform": lambda: t("Wants contact outside willhaben"),
        "email": lambda: t("E-mail address in the text"),
        "foreign_iban": lambda: t("Foreign bank account (IBAN {cc})", **p),
        "iban": lambda: t("Bank details in the text"),
        "foreign_phone": lambda: t("Foreign phone number (+{cc})", **p),
        "phone": lambda: t("Phone number in the text"),
        "messenger": lambda: t("Mentions WhatsApp/Telegram"),
        "private_dealer_text": lambda: t("Listed as private, text sounds like a dealer"),
        "dealer_private_text": lambda: t("Dealer ad, text says private sale"),
    }
    return labels[code]() if code in labels else code


def label(flag: dict[str, Any]) -> str:
    """Label in the current UI language (stored labels are in the language of the run)."""
    try:
        return _label(flag["code"], **(flag.get("params") or {}))
    except (KeyError, IndexError, ValueError):
        return flag.get("label") or flag.get("code") or ""


def _get(item: Any, *names: str) -> Any:
    for n in names:
        v = item.get(n) if isinstance(item, dict) else getattr(item, n, None)
        if v is not None:
            return v
    return None


def _iban_ok(raw: str) -> bool:
    s = raw.replace(" ", "")
    if not 15 <= len(s) <= 34:
        return False
    digits = "".join(str(int(c, 36)) for c in s[4:] + s[:4])
    return int(digits) % 97 == 1


_CC2 = set("20 27 30 31 32 33 34 36 39 40 41 43 44 45 46 47 48 49 51 52 53 54 55 56 57 58 60 61 62 63 64 65 66 "
           "81 82 84 86 90 91 92 93 94 95 98".split())


def _country_code(d: str) -> str:
    """Calling code at the start of an international number (digits only, without + / 00)."""
    if d[:1] in "17":
        return d[:1]
    return d[:2] if d[:2] in _CC2 else d[:3]


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def median_context(prices: list[float | None]) -> dict[str, Any]:
    """{median, count, p25, p75} from a list of hit prices (prices of 0/1 € are placeholders and left out)."""
    ps = sorted(p for p in prices if p and p > 1)
    out: dict[str, Any] = {"median": statistics.median(ps) if ps else None, "count": len(ps)}
    if len(ps) >= 4:
        q = statistics.quantiles(ps, n=4)
        out.update(p25=q[0], p75=q[2])
    return out


def agent_context(agent_id: int, exclude: set[int] | None = None) -> dict[str, Any]:
    """Price context of a search agent: median of its hits of the last 60 days (Claude-rejected hits left out)."""
    from sqlmodel import col, select

    from app.db import Hit, session, utcnow
    since = utcnow() - timedelta(days=CONTEXT_DAYS)
    with session() as db:
        q = select(Hit.price).where(Hit.agent_id == agent_id, col(Hit.first_seen_at) >= since,
                                    Hit.screen_relevant != False)  # noqa: E712
        if exclude:
            q = q.where(col(Hit.id).not_in(exclude))
        prices = db.exec(q).all()
    return median_context(list(prices))


def flags(item: Any, context: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Scam flags for a Hit, AdSummary, AdDetail or dict (heading, body/description, price, is_private)."""
    ctx = context or {}
    heading = _get(item, "heading", "title") or ""
    body = _get(item, "body", "description") or ""
    text = f"{heading}\n{body}"
    price = _get(item, "price")
    private = _get(item, "is_private", "private")
    out: list[dict[str, Any]] = []

    def add(code: str, severity: str, **params: Any) -> None:
        if not any(f["code"] == code for f in out):
            out.append({"code": code, "label": _label(code, **params), "severity": severity, "params": params})

    # --- price against the agent's median
    ratio = None
    median, count = ctx.get("median"), ctx.get("count") or 0
    p25, p75 = ctx.get("p25"), ctx.get("p75")
    spread_ok = not (p25 and p75) or p75 / p25 <= MAX_SPREAD
    if price and price > 1 and median and count >= MIN_SAMPLES and spread_ok:
        ratio = price / median
    cheap_ok = bool(CHEAP_OK.search(text))
    low = ratio is not None and FLOOR_RATIO <= ratio < WARN_RATIO and not cheap_ok
    very_low = low and ratio < HIGH_RATIO

    # --- brand-new luxury item at a fraction of the price
    luxury = bool(LUXURY.search(heading) and NEW_WORD.search(text) and not NOT_THE_ITEM.search(heading))
    if luxury and price and price > 1:
        floor_hit = any(rx.search(heading) and price < floor for rx, floor in LUXURY_FLOOR)
        if (ratio is not None and ratio < WARN_RATIO) or floor_hit:
            add("luxury_new_cheap", "high")
    if low and not out:
        pct, med = round(ratio * 100), f"{median:,.0f}".replace(",", ".")
        if very_low:
            add("price_far_below", "high", pct=pct, median=med)
        else:
            add("price_below", "warn", pct=pct, median=med)

    # --- payment
    m = MONEY_TRANSFER.search(text)
    if m:
        add("money_transfer", "high", what=re.sub(r"\s+", " ", m.group(0)).strip())
    if CRYPTO_PAY.search(text):
        add("crypto", "high")
    if PREPAY_ONLY.search(text) and (NO_PICKUP.search(text) or not PICKUP_OK.search(text)):
        add("prepay_only", "warn")
    for raw in IBAN.findall(text.upper()):
        if _iban_ok(raw):
            if raw[:2] == "AT":
                add("iban", "info")
            else:
                add("foreign_iban", "warn" if raw[:2] in ("DE", "CH") else "high", cc=raw[:2])

    # --- delivery
    if COURIER.search(text):
        if ABROAD.search(text):
            add("courier_abroad", "warn")
        elif low:
            add("courier_cheap", "warn")
    if low and SHIP_ONLY.search(text):
        add("ship_only_cheap", "warn")

    # --- leaving the platform
    if OFF_PLATFORM.search(text):
        add("off_platform", "warn")
    if EMAIL.search(text) or EMAIL_SPELLED.search(text):
        add("email", "warn")
    phone_kind = None
    no_iban = IBAN.sub(" ", text.upper())          # IBAN digit groups are not phone numbers
    for m in PHONE_INTL.finditer(no_iban):
        d = _digits(m.group(0))
        d = d[2:] if m.group(0).startswith("00") else d
        if 9 <= len(d) <= 15:
            if d.startswith(NEAR_CC):
                phone_kind = phone_kind or "phone"
            else:
                add("foreign_phone", "warn", cc=_country_code(d))
                phone_kind = "foreign"
    if phone_kind is None:
        for m in PHONE_NAT.finditer(no_iban):
            if 9 <= len(_digits(m.group(0))) <= 14:
                phone_kind = "phone"
                break
    if phone_kind == "phone":
        add("phone", "info")
    if MESSENGER.search(text) and not any(f["code"] == "off_platform" for f in out):
        add("messenger", "info")

    # --- dealer / private mismatch
    if private is True and DEALER_TEXT.search(text):
        add("private_dealer_text", "info")
    elif private is False and PRIVATE_TEXT.search(text):
        add("dealer_private_text", "info")

    out.sort(key=lambda f: -RANK[f["severity"]])
    return out


def level(fl: list[dict[str, Any]] | None) -> str | None:
    """Highest severity, or None without flags."""
    return max((f.get("severity", "info") for f in fl or []), key=lambda s: RANK.get(s, 0), default=None)


def warnings(fl: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Flags worth a badge/push (warn + high), with labels in the current language."""
    return [{**f, "label": label(f)} for f in fl or [] if RANK.get(f.get("severity"), 0) >= RANK["warn"]]


def all_flags(fl: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [{**f, "label": label(f)} for f in fl or []]


def headline(fl: list[dict[str, Any]] | None) -> str:
    """Top warning label for a push ("" when nothing is worth a warning)."""
    w = warnings(fl)
    return w[0]["label"] if w else ""


def for_viewer(d: Any, h: Any = None) -> list[dict[str, Any]]:
    """Ad viewer: re-check with the full description of the detail page; falls back to the stored hit flags."""
    if d is None:
        return all_flags(getattr(h, "scam_flags", None))
    ctx = agent_context(h.agent_id, {h.id}) if h is not None and getattr(h, "agent_id", None) else None
    item = {"heading": d.heading, "description": d.description, "price": d.price,
            "is_private": d.is_private if d.is_private is not None else getattr(h, "is_private", None)}
    return all_flags(flags(item, ctx))
