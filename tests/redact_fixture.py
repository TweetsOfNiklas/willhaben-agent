"""Redact personal data from a captured willhaben JSON before it becomes a test fixture.

    python tests/redact_fixture.py tests/fixtures/<file>.json [...]

Replaces seller/contact names, street addresses, exact coordinates, seller/org ids, every UUID
(including willhaben's tracking id of the account that made the request) and embedded API keys.
Deterministic: the same input value always maps to the same placeholder, so cross-references
inside a fixture (e.g. seller uuid in several places) stay consistent. Review the diff afterwards.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import uuid
from pathlib import Path

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
NAME_ATTRS = {"CONTACT/NAME": "Max Mustermann", "ORGNAME": "Beispiel Shop"}
NAME_KEYS = {"contactName", "contactFirstName", "contactLastName", "phoneNo", "phone", "email"}
SECRET_KEYS = re.compile(r"(?i)api[_-]?key|token|secret")


def _fake_uuid(value: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "wh-fixture:" + value))


def _fake_id(value: str) -> str:
    return str(10_000_000 + int(hashlib.sha256(value.encode()).hexdigest(), 16) % 9_000_000)


def redact(data):
    ids: dict[str, str] = {}
    names: dict[str, str] = {}   # real name -> placeholder, replaced everywhere afterwards

    def walk(o, key=""):
        if isinstance(o, dict):
            # search results: attribute lists {name, values}
            if "name" in o and "values" in o and isinstance(o["values"], list):
                n = o["name"]
                if n in NAME_ATTRS:
                    names.update({v: NAME_ATTRS[n] for v in o["values"] if v})
                    o["values"] = [NAME_ATTRS[n]]
                elif n == "ADDRESS":
                    o["values"] = ["Musterstraße 1"]
                elif n == "COORDINATES":
                    o["values"] = [",".join(f"{float(x):.1f}" for x in v.split(",")) for v in o["values"]]
                elif n in ("ORGID",):
                    o["values"] = [ids.setdefault(v, _fake_id(v)) for v in o["values"]]
                elif n == "ORGMAINLOGO":
                    o["values"] = ["atz/example/logo.png"]
            # ad detail: contact fields {id, description, value}
            if o.get("id") in NAME_KEYS and o.get("value"):
                names[str(o["value"])] = "Erika"
                o["value"] = "Erika"
            for k, v in list(o.items()):
                if SECRET_KEYS.search(k) and isinstance(v, str):
                    o[k] = "REDACTED"
                elif k == "sellerProfileUserData" and isinstance(v, dict) and v.get("name"):
                    names[v["name"]] = "Erika"
                    v["name"] = "Erika"
                    walk(v, k)
                elif k == "organisationDetails" and isinstance(v, dict):
                    for idk in ("id",):
                        if v.get(idk) is not None:
                            ids.setdefault(str(v[idk]), _fake_id(str(v[idk])))
                    walk(v, k)
                else:
                    walk(v, k)
        elif isinstance(o, list):
            for v in o:
                walk(v, key)

    walk(data)
    text = json.dumps(data, ensure_ascii=False, indent=1)
    text = UUID_RE.sub(lambda m: _fake_uuid(m.group(0)), text)
    for real, fake in names.items():   # e.g. seller_name in tracking data, PHONECONTACT entries
        text = text.replace(json.dumps(real, ensure_ascii=False), json.dumps(fake, ensure_ascii=False))
    for real, fake in ids.items():   # ids also appear inside URLs and other fields
        text = re.sub(rf"(?<!\d){re.escape(real)}(?!\d)", fake, text)
    return text, ids


def main(paths: list[str]) -> None:
    for p in map(Path, paths):
        text, ids = redact(json.loads(p.read_text(encoding="utf-8")))
        p.write_text(text + "\n", encoding="utf-8")
        print(f"{p}: redacted, id map {ids}")


if __name__ == "__main__":
    main(sys.argv[1:])
