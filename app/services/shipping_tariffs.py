"""Domestic (Austria → Austria) parcel tariffs for private customers, online label prices.

There is no price API for private customers, so these are the carriers' published price lists (checked
2026-10-09 on post.at, gls-one.at, myhermes.at, mydpd.at). Where a carrier's own pages disagree, the higher price
is used; the binding price is the one in the carrier's checkout. Each carrier measures size differently
("longest + shortest side", girth, or max dimensions); `fits()` applies the rule of the option.
Update `CHECKED` and the numbers when a carrier changes its prices.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

CHECKED = "2026-10-09"


@dataclass(frozen=True)
class Option:
    carrier: str
    size: str                     # carrier's size name, e.g. "S"
    price: float                  # EUR, online label
    max_kg: float
    rule: str                     # "ls" longest+shortest side | "girth" L + 2W + 2H | "dims" each side
    limit: tuple[float, ...]      # ls/girth: (max sum, max longest side); dims: (L, W, H)
    delivery: str = "home"        # home | shop (to a parcel shop/locker, recipient picks up)
    note: str = ""
    online: bool = True           # label can be created online (False: buy it at the counter)

    @property
    def key(self) -> str:
        return f"{self.carrier}:{self.size}:{self.delivery}"

    def label(self) -> str:
        name = CARRIERS[self.carrier].name
        return f"{name} {self.size}" + (" → Shop" if self.delivery == "shop" else "")

    def fits(self, parcel: dict[str, Any]) -> bool:
        dims = sorted((float(parcel.get(k) or 0) for k in ("length_cm", "width_cm", "height_cm")), reverse=True)
        if float(parcel.get("weight_kg") or 0) > self.max_kg:
            return False
        lo, mid, hi = dims[2], dims[1], dims[0]
        if self.rule == "ls":
            return hi + lo <= self.limit[0] and (len(self.limit) < 2 or hi <= self.limit[1])
        if self.rule == "girth":
            return hi + 2 * mid + 2 * lo <= self.limit[0] and (len(self.limit) < 2 or hi <= self.limit[1])
        lim = sorted(self.limit, reverse=True)
        return hi <= lim[0] and mid <= lim[1] and lo <= lim[2]


@dataclass(frozen=True)
class Carrier:
    name: str
    label_url: str                # where the online label flow starts
    dropoff: str                  # where the parcel is handed in
    login: str                    # what account the flow needs
    options: tuple[Option, ...] = field(default_factory=tuple)
    verified: bool = False        # prices checked against the carrier's own page


def _gls() -> tuple[Option, ...]:
    # gls-one.at/de/parcel: longest + shortest side; to a PaketShop/PaketBox 1 € less; 31.5 kg
    out = []
    for size, rule, limit, price in [("XS", "ls", (35,), 4.20), ("S", "ls", (50,), 4.90), ("M", "ls", (65,), 7.20),
                                     ("L", "ls", (80,), 9.30), ("XL", "dims", (200, 80, 60), 13.40)]:
        out.append(Option("gls", size, price, 31.5, rule, limit))
        out.append(Option("gls", size, round(price - 1, 2), 31.5, rule, limit, delivery="shop"))
    return tuple(out)


def _dpd() -> tuple[Option, ...]:
    # mydpd.at/preise (higher of the two official price sets): shortest + longest side, 31.5 kg, to a Pickup shop −0.30 €
    out = []
    for size, ls, price in [("S", 50, 4.70), ("M", 70, 6.70), ("L", 90, 8.70)]:
        out.append(Option("dpd", size, price, 31.5, "ls", (ls,)))
        out.append(Option("dpd", size, round(price - 0.3, 2), 31.5, "ls", (ls,), delivery="shop"))
    out.append(Option("dpd", "XL", 12.70, 31.5, "girth", (300, 175)))
    return tuple(out)


CARRIERS: dict[str, Carrier] = {
    "post": Carrier(
        name="Post", label_url="https://www.post.at/p/a/paketmarke",
        dropoff="Postfiliale, Post Partner, SB-Versandstation; Abholservice", login="post.at Online Services",
        options=(
            # online Paketmarke: longest + shortest side, ≤10 kg, tracking + 510 € liability, print at home (A5)
            Option("post", "PM 45", 5.21, 10, "ls", (45,)),
            Option("post", "PM 70", 7.83, 10, "ls", (70,)),
            Option("post", "PM 120", 10.47, 10, "ls", (120,)),
            # counter only (Tarifbroschüre Privat 01.08.2026)
            Option("post", "Päckchen S", 3.10, 2, "dims", (35.3, 25, 3), note="am Postschalter, ohne Online-Label",
                   online=False),
            Option("post", "Päckchen M", 4.65, 2, "girth", (90, 50), note="am Postschalter, ohne Online-Label",
                   online=False),
        ),
        verified=True,
    ),
    "gls": Carrier(
        name="GLS", label_url="https://www.gls-one.at/de/parcel/",
        dropoff="GLS PaketShop (QR-Code, ohne Drucker)", login="optional (Gast)", options=_gls(), verified=True,
    ),
    "hermes": Carrier(
        name="Hermes", label_url="https://www.myhermes.at/privatkunden/sendung-aufgeben/online-paketschein-erzeugen/",
        dropoff="Hermes PaketShop (bar bezahlen, Schein ausgedruckt)", login="optional",
        options=tuple(Option("hermes", size, price, 31.5, "ls", (ls,), note="Bezahlung bar im PaketShop")
                      for size, ls, price in [("S", 50, 4.60), ("M", 80, 6.60), ("L", 120, 9.60)]),
        verified=True,
    ),
    "dpd": Carrier(
        name="DPD", label_url="https://www.mydpd.at/paket-versenden",
        dropoff="DPD Pickup Paketshop (QR-Code, ohne Drucker)", login="optional (Gast)", options=_dpd(),
        verified=True,
    ),
}


def options_for(parcel: dict[str, Any], carriers: list[str]) -> list[Option]:
    """Per carrier and delivery type only the smallest fitting size, all sorted by price."""
    best: dict[tuple[str, str], Option] = {}
    for c in carriers:
        for o in CARRIERS[c].options:
            if o.fits(parcel):
                k = (o.carrier, o.delivery)
                if k not in best or o.price < best[k].price:
                    best[k] = o
    return sorted(best.values(), key=lambda o: (o.price, o.carrier))
