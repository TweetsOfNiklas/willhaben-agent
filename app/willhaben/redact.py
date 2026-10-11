"""Remove personal data from recorded willhaben answers before they become test fixtures.

Used by app/willhaben/export.py (recordings -> fixture files) and tests/redact_fixture.py (one JSON file by hand).

Replaced: seller/contact/partner names, e-mail addresses, phone numbers, street addresses, IBANs, exact coordinates,
seller/org ids, user / conversation / message ids, every UUID, chat message texts (numbered "Nachricht 1", …, unless
kept on purpose), API keys and tokens. Public ad ids, prices, titles and categories stay: tests need them.
Deterministic: one value always maps to the same placeholder within one export, so cross-references (a conversation id
in the list and in the message URL) stay consistent. Review the result before committing it.

Anonymous modules import this file: it must never import the logged-in session (tests/test_anonymous_public.py).
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[0-9A-Z]{4}){3,7}(?:\s?[0-9A-Z]{1,4})?\b")
PHONE_RE = re.compile(r"(?<![\d.])(?:\+43|0043|0)\s?6\d{1,3}[\s/-]?\d{3,}(?:[\s-]?\d{2,4}){0,2}(?!\d)")
NEXT_DATA_RE = re.compile(r'(<script id="__NEXT_DATA__" type="application/json">)(.*?)(</script>)', re.S)

NAME_ATTRS = {"CONTACT/NAME": "Max Mustermann", "ORGNAME": "Beispiel Shop"}
NAME_KEYS = {"contactName", "contactFirstName", "contactLastName", "phoneNo", "phone", "email"}   # detail contact ids
SECRET_KEYS = re.compile(r"(?i)api[_-]?key|token|secret|password")
PERSON_KEYS = {"emailAddress": "erika@example.org", "email": "erika@example.org", "firstName": "Erika",
               "lastName": "Muster", "nickname": "Erika M.", "displayName": "Erika M.", "partnerName": "Erika M.",
               "partner_name": "Erika M.", "userName": "erika", "username": "erika", "contactName": "Erika M.",
               "fullName": "Erika Muster", "full_name": "Erika Muster", "contact-name": "Erika M.",
               "contact-name-initials": "EM", "store-title": "Beispiel Shop"}
PERSON_PARENTS = {"participant_info", "participantInfo", "participants", "partner", "sender", "receiver", "recipient",
                  "user", "userData", "seller", "buyer", "sellerProfileUserData", "otherParty", "counterpart"}
ADDRESS_KEYS = {"street", "street-name", "streetName", "street_name", "houseNumber", "house_number", "address", "addressLine1",
                "addressLine2", "address_line1", "address_line2"}
IBAN_KEYS = {"iban", "IBAN", "bic", "BIC", "accountNumber"}
ID_KEYS = {"loginId", "userId", "user_id", "uuid", "conversationId", "conversation_id", "messageId", "message_id",
           "senderId", "sender_id", "receiverId", "receiver_id", "partnerId", "partner_id", "participantId",
           "participant_id", "orgId", "organisationId", "customerId", "customer_id", "user-id", "store-id"}
MESSAGE_MARKERS = {"conversation_id", "conversationId", "message_id", "messageId", "sender", "senderId", "sender_id",
                   "isOwnMessage", "messageType", "message_type", "req_type", "participant_info"}
MESSAGE_PARENTS = {"lastMessage", "last_message", "messages", "message", "payload"}
ID_PARENTS = {"conversations", "participants", "participant_info", "participantInfo", "sender", "receiver", "partner"}
TEXT_KEYS = {"text", "body", "messageText", "message_text", "preview", "content"}


def _fake_uuid(value: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "wh-fixture:" + value))


def _fake_id(value: str) -> str:
    return str(10_000_000 + int(hashlib.sha256(value.encode()).hexdigest(), 16) % 9_000_000)


def _fake_any(value: str) -> str:
    """A placeholder in the value's own format: uuid -> uuid, digits -> digits, else a short hash."""
    if UUID_RE.fullmatch(value):
        return _fake_uuid(value)
    if value.isdigit():
        return _fake_id(value)
    return "id-" + hashlib.sha256(value.encode()).hexdigest()[:10]


@dataclass
class RedactContext:
    """One per export: placeholders stay consistent across all files."""
    keep_texts: bool = False
    ids: dict[str, str] = field(default_factory=dict)       # real id -> fake id
    names: dict[str, str] = field(default_factory=dict)     # real name / e-mail -> placeholder
    texts: dict[str, str] = field(default_factory=dict)     # real chat text -> "Nachricht n"

    def text_for(self, real: str) -> str:
        if real not in self.texts:
            self.texts[real] = f"Nachricht {len(self.texts) + 1}"
        return self.texts[real]

    def id_for(self, real: Any) -> Any:
        s = str(real)
        fake = self.ids.setdefault(s, _fake_any(s))
        return int(fake) if isinstance(real, int) and fake.isdigit() else fake


def _is_message(o: dict[str, Any], parent: str) -> bool:
    return bool(MESSAGE_MARKERS & o.keys()) or parent in MESSAGE_PARENTS


def redact_json(data: Any, ctx: RedactContext | None = None) -> Any:
    """A redacted copy of a JSON value (dict/list/scalar)."""
    ctx = ctx or RedactContext()
    data = copy.deepcopy(data)

    def walk(o: Any, parent: str = "") -> None:
        if isinstance(o, dict):
            # search results: attribute lists {name, values}
            if "name" in o and "values" in o and isinstance(o["values"], list):
                n = o["name"]
                if n in NAME_ATTRS:
                    ctx.names.update({str(v): NAME_ATTRS[n] for v in o["values"] if v})
                    o["values"] = [NAME_ATTRS[n]]
                elif n == "ADDRESS":
                    o["values"] = ["Musterstraße 1"]
                elif n == "COORDINATES":
                    o["values"] = [",".join(f"{float(x):.1f}" for x in str(v).split(",")) for v in o["values"]]
                elif n == "ORGID":
                    o["values"] = [ctx.ids.setdefault(str(v), _fake_id(str(v))) for v in o["values"]]
                elif n == "ORGMAINLOGO":
                    o["values"] = ["atz/example/logo.png"]
            # ad detail: contact fields {id, description, value}
            if o.get("id") in NAME_KEYS and o.get("value"):
                ctx.names[str(o["value"])] = "Erika"
                o["value"] = "Erika"
            message = _is_message(o, parent)
            for k, v in list(o.items()):
                if isinstance(v, str) and SECRET_KEYS.search(k):
                    o[k] = "REDACTED"
                elif k in PERSON_KEYS and isinstance(v, str) and v:
                    ctx.names[v] = PERSON_KEYS[k]
                    o[k] = PERSON_KEYS[k]
                elif k == "name" and parent in PERSON_PARENTS and isinstance(v, str) and v:
                    ctx.names[v] = "Erika M."
                    o[k] = "Erika M."
                elif "phone" in k.lower() and isinstance(v, str) and v:
                    ctx.names[v] = "0660 0000000"
                    o[k] = "0660 0000000"
                elif k in ADDRESS_KEYS and isinstance(v, str) and v:
                    ctx.names[v] = "Musterstraße 1"
                    o[k] = "Musterstraße 1"
                elif k in IBAN_KEYS and isinstance(v, str) and v:
                    o[k] = "AT00 0000 0000 0000 0000"
                elif (k in ID_KEYS or (k == "id" and (message or parent in ID_PARENTS))) \
                        and isinstance(v, (str, int)) and not isinstance(v, bool) \
                        and str(v):
                    o[k] = ctx.id_for(v)
                elif k in TEXT_KEYS and message and isinstance(v, str) and v and not ctx.keep_texts:
                    o[k] = ctx.text_for(v)
                elif k == "sellerProfileUserData" and isinstance(v, dict) and v.get("name"):
                    ctx.names[v["name"]] = "Erika"
                    v["name"] = "Erika"
                    walk(v, k)
                elif k == "organisationDetails" and isinstance(v, dict):
                    if v.get("id") is not None:
                        ctx.ids.setdefault(str(v["id"]), _fake_id(str(v["id"])))
                    walk(v, k)
                else:
                    walk(v, k)
        elif isinstance(o, list):
            for v in o:
                walk(v, parent)

    walk(data)
    return data


def redact_text(text: str, ctx: RedactContext) -> str:
    """Pass over any text (serialized JSON, HTML, URLs): known values, UUIDs, e-mails, IBANs, phone numbers."""
    if not text:
        return text
    for real, fake in sorted(ctx.texts.items(), key=lambda kv: -len(kv[0])):
        if len(real) >= 4:
            text = text.replace(json.dumps(real, ensure_ascii=False)[1:-1], fake)
    for real, fake in sorted(ctx.names.items(), key=lambda kv: -len(kv[0])):
        if len(real) >= 3:
            text = text.replace(real, fake)
    text = UUID_RE.sub(lambda m: ctx.ids.setdefault(m.group(0), _fake_uuid(m.group(0))), text)
    for real, fake in ctx.ids.items():      # ids also appear inside URLs and other fields
        if real.isdigit() and len(real) >= 5:
            text = re.sub(rf"(?<!\d){re.escape(real)}(?!\d)", fake, text)
        elif not real.isdigit() and len(real) >= 6:
            text = text.replace(real, fake)
    text = EMAIL_RE.sub("erika@example.org", text)
    text = IBAN_RE.sub(lambda m: "AT00 0000 0000 0000 0000" if m.group(0)[:2].isalpha() and m.group(0)[:2].isupper() else m.group(0), text)
    return PHONE_RE.sub("0660 0000000", text)


def redact_html(html: str, ctx: RedactContext) -> str:
    """A page: its __NEXT_DATA__ JSON redacted like any JSON, then the text pass over the whole page."""
    def sub(m: re.Match[str]) -> str:
        try:
            data = redact_json(json.loads(m.group(2)), ctx)
        except ValueError:
            return m.group(0)
        return m.group(1) + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + m.group(3)
    return redact_text(NEXT_DATA_RE.sub(sub, html), ctx)


FIXTURE_HEADERS = {"accept", "content-type", "x-wh-client", "location", "content-length"}


def headers_for_fixture(headers: dict[str, Any] | None) -> dict[str, str]:
    """Only the headers a fixture needs; everything else (cookies, tracking, server names) is left out."""
    return {k: str(v) for k, v in (headers or {}).items() if k.lower() in FIXTURE_HEADERS}


def redact(data: Any) -> tuple[str, dict[str, str]]:
    """One JSON document -> (redacted, pretty-printed text, id map). tests/redact_fixture.py uses this."""
    ctx = RedactContext(keep_texts=True)
    clean = redact_json(data, ctx)
    return redact_text(json.dumps(clean, ensure_ascii=False, indent=1), ctx), ctx.ids
