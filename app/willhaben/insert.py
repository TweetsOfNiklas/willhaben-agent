"""Placing a marketplace ad (free private ad, "bap", adTypeId/productId 67) like willhaben's own form.

Taken from the web app bundle /bbx-adin (2026-10-09); every call runs as fetch() in the logged-in page.

  form      GET  /iad/anzeigenaufgabe/marktplatz?adTypeId=67&productId=67   (HTML, __NEXT_DATA__)
            -> pageProps.ad (template incl. location/contact), categoryTree, prefillAttributes
  category  GET  /webapi/iad/bap/categorysuggestion/attributetree?title=…&adTypeId=67&productId=67
  presave   POST /webapi/iad/bap/presave           ad JSON -> ad with adId, adUuid, contextLinkList (draft)
  images    POST /webapi/iad-upload/atimage/temp   multipart "file" -> {reference, …}
            PUT  /webapi/iad-upload<imageList>/order-and-associate  {advertImage:[{reference}], floorPlans:[]}
  save      POST /webapi/iad/bap/save              ad JSON (with adId)
  cart      POST /webapi/iad/products/privates/atz/<adUuid>/carts  {currency:"EUR"} -> {shoppingCartId, status:"Open"}
            PUT  /webapi/iad/products/privates/carts/<id>/items     {productReferences: []} -> {totalPrice}
            POST /webapi/iad/products/privates/carts/<id>/checkout  {billingData: null} -> {status:"ACTIVATED"}

This module stops if anything would cost money (totalPrice > 0 or a paymentUrl) - nothing is ever paid.
"""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

from app.willhaben.parse import extract_next_data
from app.willhaben.session import BrowserSession, NotLoggedIn

log = logging.getLogger(__name__)

AD_TYPE = PRODUCT = 67          # "Marktplatz – kostenlose Anzeige" (private)
MAX_PHOTOS = 15
FORM_PATH = f"/iad/anzeigenaufgabe/marktplatz?adTypeId={AD_TYPE}&productId={PRODUCT}"


class InsertError(RuntimeError):
    pass


async def load_form(s: BrowserSession) -> dict[str, Any]:
    """pageProps of the empty insertion form: ad template, category tree with required attributes."""
    r = await s.api("GET", FORM_PATH, accept="text/html")
    nd = extract_next_data(r.text) if r.ok else None
    if not nd or nd.get("page") != "/iad/anzeigenaufgabe/marktplatz":
        raise NotLoggedIn(f"Anzeigenformular nicht erreichbar (HTTP {r.status}, eingeloggt?)")
    return (nd.get("props") or {}).get("pageProps") or {}


async def suggest_categories(s: BrowserSession, title: str) -> Any:
    path = (f"/webapi/iad/bap/categorysuggestion/attributetree?title={quote(title)}"
            f"&adTypeId={AD_TYPE}&productId={PRODUCT}")
    return await s.api_json("GET", path)


def category_index(tree: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """code -> {label, path (labels root..node), codes, required attribute codes, leaf}."""
    out: dict[str, dict[str, Any]] = {}

    def walk(node: dict[str, Any], labels: list[str], codes: list[str], inherited: list[dict[str, Any]]):
        refs = inherited + [r for r in node.get("attributeReferences") or [] if isinstance(r, dict)]
        here_labels, here_codes = labels + [node.get("label") or ""], codes + [node.get("code") or ""]
        children = node.get("children") or []
        if node.get("code"):
            out[node["code"]] = {"label": node.get("label"), "path": here_labels[1:], "codes": here_codes[1:],
                                 "required": [r["code"] for r in refs if r.get("required")],
                                 "attributes": refs, "leaf": not children}
        for ch in children:
            walk(ch, here_labels, here_codes, refs)

    walk(tree.get("categoryNode") or {}, [], [], [])
    return out


def attribute_values(tree: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """attribute code -> {label, values: {code: label}} from the category tree."""
    out = {}
    for a in tree.get("attributes") or []:
        el = a.get("treeAttributeElement") or {}
        if el.get("code"):
            out[el["code"]] = {"label": el.get("label"),
                               "values": {v.get("code"): v.get("label") for v in a.get("values") or [] if v.get("code")}}
    return out


async def get_own_ad(s: BrowserSession, ad_id: str) -> dict[str, Any]:
    """An own ad in the editor's JSON format (read-only), the same shape presave/save take."""
    return await s.api_json("GET", f"/webapi/iad/bap/{int(ad_id)}")


# ------------------------------------------------------------------ plan (read-only)
PICKUP = {"label": "Selbstabholung", "code": "PICKUP"}
HANDOVER = "Uebergabe"


def suggestion_paths(raw: Any) -> list[list[dict[str, str]]]:
    """Category suggestions as categoryPath lists [{label, code}, …] root → leaf, without duplicates."""
    out, seen = [], set()
    for top in (raw or {}).get("categories") or []:
        path, node = [], top
        while isinstance(node, dict):
            path.append({"label": node.get("name") or node.get("label") or "", "code": node.get("code") or ""})
            kids = node.get("categories") or []
            node = kids[0] if kids else None
        if path and path[-1]["code"] not in seen:
            seen.add(path[-1]["code"])
            out.append(path)
    return out


def _norm(s: str | None) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def guess_value(attr_label: str, values: dict[str, str], draft_attrs: list[dict[str, Any]], condition: str | None) -> str | None:
    """Pre-select a value code from what Claude wrote (condition, attributes like Marke/Farbe)."""
    by_label = {_norm(lbl): code for code, lbl in values.items()}
    wanted = []
    if _norm(attr_label) == "zustand" and condition:
        wanted.append(condition)
    wanted += [a.get("value") for a in draft_attrs or [] if _norm(a.get("name")) == _norm(attr_label)]
    for w in wanted:
        n = _norm(w)
        if n in by_label:
            return by_label[n]
        hit = next((code for lbl, code in by_label.items() if n and (n in lbl or lbl in n)), None)
        if hit:
            return hit
    return None


def build_plan(form: dict[str, Any], raw_suggestions: Any, *, condition: str | None,
               attributes: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Everything the user confirms before publishing: category options and required fields per option."""
    tree = form.get("categoryTree") or {}
    cats, vals = category_index(tree), attribute_values(tree)
    options = []
    for path in suggestion_paths(raw_suggestions)[:5]:
        info = cats.get(path[-1]["code"]) or {}
        fields = []
        for code in dict.fromkeys(info.get("required") or []):
            if code == HANDOVER:
                continue                       # always "Selbstabholung" in this version
            a = vals.get(code) or {"label": code, "values": {}}
            fields.append({"code": code, "label": a["label"], "values": list(a["values"].items()),
                           "selected": guess_value(a["label"], a["values"], attributes or [], condition)})
        options.append({"path": path, "label": " › ".join(p["label"] for p in path), "fields": fields})
    ad = form.get("ad") or {}
    return {"options": options, "location": " ".join(x for x in (ad.get("postCode"), ad.get("location")) if x),
            "contact": " ".join(x for x in (ad.get("firstName"), ad.get("surName")) if x)}


# ------------------------------------------------------------------ publish (writes, only on the user's click)
def description_html(text: str) -> str:
    """Plain text → the editor's HTML (paragraphs, line breaks, "• " lines as list)."""
    import html as _html

    blocks = []
    for para in [p for p in (text or "").replace("\r\n", "\n").split("\n\n") if p.strip()]:
        lines = [ln for ln in para.split("\n") if ln.strip()]
        if lines and all(ln.lstrip().startswith(("• ", "- ")) for ln in lines):
            items = "".join(f"<li>{_html.escape(ln.lstrip()[2:].strip())}</li>" for ln in lines)
            blocks.append(f"<ul>{items}</ul>")
        else:
            blocks.append("<p>" + "<br>".join(_html.escape(ln.strip()) for ln in lines) + "</p>")
    return "".join(blocks)


def _link(ad: dict[str, Any], *ids: str) -> dict[str, Any] | None:
    links = {link.get("id"): link for link in (ad.get("contextLinkList") or {}).get("contextLink") or []}
    return next((links[i] for i in ids if links.get(i, {}).get("relativePath")), None)


def _webapi(link: dict[str, Any], upload: bool = False) -> str:
    svc = link.get("serviceName") or "iad"
    if upload and svc == "iad":
        svc = "iad-upload"
    return f"/webapi/{svc}{link['relativePath']}"


def compose_ad(template: dict[str, Any], *, heading: str, description: str, price: float | None,
               path: list[dict[str, str]], attrs: dict[str, tuple[str, str]], attr_labels: dict[str, str]) -> dict[str, Any]:
    import copy

    ad = copy.deepcopy(template)
    ad["heading"] = heading.strip()[:80]
    ad["description"] = description_html(description)
    ad["price"] = (int(price) if price is not None and float(price).is_integer() else price)
    ad["categoryPath"] = {"path": path}
    tree_attrs = [{"attributeNode": {"label": attr_labels.get(code, code), "code": code},
                   "selectedAttributeValueNodes": [{"label": label, "code": value}]}
                  for code, (value, label) in attrs.items()]
    tree_attrs.append({"attributeNode": {"label": "Übergabe", "code": HANDOVER}, "selectedAttributeValueNodes": [PICKUP]})
    ad["treeAttributes"] = tree_attrs
    ad.pop("treeAttributeIds", None)
    return ad


STEPS = ("presave", "photos", "save", "cart")   # stop_after: halt before the ad goes public (checkout)


def _shape(obj: Any) -> Any:
    """Structure of a response without personal data: keys, link ids, a few status fields."""
    if not isinstance(obj, dict):
        return type(obj).__name__
    links = [link.get("id") for link in (obj.get("contextLinkList") or {}).get("contextLink") or []]
    keep = {k: obj.get(k) for k in ("adId", "adUuid", "status", "totalPrice", "shoppingCartId", "productId",
                                      "paymentUrl", "advertStatus") if k in obj}
    return {"keys": sorted(obj), "links": links, **keep}


async def publish(s: BrowserSession, ad: dict[str, Any], photos: list[tuple[str, bytes, str]],
                  existing_ad_id: str | None = None, on_presaved=None, stop_after: str | None = None) -> dict[str, Any]:
    """presave → photos → save → empty cart → checkout. Returns {ad_id, ad_uuid, url}.

    stop_after ("presave" | "photos" | "save" | "cart") returns {"stopped": step, ...} with the response
    structures instead; everything before checkout stays a non-public willhaben draft."""
    trace: dict[str, Any] = {}
    if existing_ad_id:          # a former attempt already created the willhaben draft: continue with it
        base = await get_own_ad(s, existing_ad_id)
        ad = {**ad, "adId": base.get("adId"), "adUuid": base.get("adUuid"), "contextLinkList": base.get("contextLinkList")}
    else:
        presave = _link(ad, "presave")
        if not presave:
            raise InsertError("Formular ohne presave-Link")
        pre = await s.api_json("POST", _webapi(presave), ad)
        ad = {**ad, "adId": pre.get("adId"), "adUuid": pre.get("adUuid"), "contextLinkList": pre.get("contextLinkList"),
              "productId": pre.get("productId") or ad.get("productId"), "taggingData": pre.get("taggingData") or ad.get("taggingData")}
        trace["presave"] = _shape(pre)
        if on_presaved and ad.get("adId"):
            on_presaved(str(ad["adId"]))
    if not ad.get("adId"):
        raise InsertError("willhaben hat keine Anzeigen-ID vergeben")
    if stop_after == "presave":
        return {"stopped": "presave", "ad_id": str(ad["adId"]), "trace": trace}

    if photos:
        refs = []
        for name, data, mime in photos:
            r = await s.upload("/webapi/iad-upload/atimage/temp", "file", name, data, mime)
            if not r.ok:
                raise InsertError(f"Foto-Upload {name}: HTTP {r.status} {r.text[:150]}")
            refs.append({"reference": (r.json() or {}).get("reference")})
        img = _link(ad, "imageList")
        if not img:
            raise InsertError("kein imageList-Link nach dem Vorspeichern")
        assoc = await s.api_json("PUT", _webapi(img, upload=True) + "/order-and-associate",
                                 {"advertImage": refs, "floorPlans": []})
        trace["photos"] = {"uploaded": len(refs), "associate": _shape(assoc)}
    if stop_after == "photos":
        return {"stopped": "photos", "ad_id": str(ad["adId"]), "trace": trace}

    save = _link(ad, "selfLinkFinish", "selfLinkSave")
    if not save:
        raise InsertError("kein Speichern-Link")
    saved = await s.api_json("POST", _webapi(save), ad)
    ad_id, ad_uuid = str(saved.get("adId") or ad["adId"]), saved.get("adUuid") or ad.get("adUuid")
    trace["save"] = _shape(saved)
    if stop_after == "save":
        return {"stopped": "save", "ad_id": ad_id, "trace": trace}

    cart = await s.api_json("POST", f"/webapi/iad/products/privates/atz/{ad_uuid}/carts", {"currency": "EUR"})
    cart_id = cart.get("shoppingCartId")
    items = await s.api_json("PUT", f"/webapi/iad/products/privates/carts/{cart_id}/items", {"productReferences": []})
    trace["cart"] = {"cart": _shape(cart), "items": _shape(items)}
    if (items.get("totalPrice") or 0) > 0 or items.get("paymentUrl"):
        raise InsertError(f"willhaben verlangt {items.get('totalPrice')} € – abgebrochen, nichts bezahlt")
    if stop_after == "cart":
        return {"stopped": "cart", "ad_id": ad_id, "trace": trace}
    done = await s.api_json("POST", f"/webapi/iad/products/privates/carts/{cart_id}/checkout", {"billingData": None})
    if (done or {}).get("status") != "ACTIVATED":
        raise InsertError(f"Anzeige nicht aktiviert (Status {(done or {}).get('status')}"
                          f"{', Zahlung verlangt' if (done or {}).get('paymentUrl') else ''}) – nichts bezahlt")
    trace["checkout"] = _shape(done)
    return {"ad_id": ad_id, "ad_uuid": ad_uuid, "url": f"https://www.willhaben.at/iad/object?adId={ad_id}", "trace": trace}
