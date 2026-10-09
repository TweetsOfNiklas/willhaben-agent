"""IPP/1.1 over HTTP (RFC 8010/8011): the few operations needed to print a PDF, in pure Python.

Wire format: a message starts with an 8-byte header (version 1.1, then the operation id in a request or the status
code in a response, then the 4-byte request id). Attribute groups follow (0x01 operation, 0x02 job, 0x04 printer,
0x05 unsupported). Each attribute is a tag byte, a 2-byte name length and name, a 2-byte value length and value; a
further value of the same attribute repeats the tag with an empty name. The group list ends with the tag 0x03, and
a Print-Job request carries the PDF bytes after it. Collections (media-col) are a begin tag, then per member a
"memberAttrName" attribute (tag 0x4A, value = member name) followed by the member's value, then the end tag 0x37.
"""
from __future__ import annotations

import random
import re
import struct
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.i18n import t

PRINT_JOB = 0x0002
GET_JOB_ATTRIBUTES = 0x0009
GET_PRINTER_ATTRIBUTES = 0x000B

G_END, G_OPERATION, G_JOB, G_PRINTER, G_UNSUPPORTED = 0x03, 0x01, 0x02, 0x04, 0x05
T_INTEGER, T_BOOLEAN, T_ENUM, T_OCTETSTRING = 0x21, 0x22, 0x23, 0x30
T_TEXT, T_NAME, T_KEYWORD, T_URI, T_CHARSET, T_LANGUAGE, T_MIME = 0x41, 0x42, 0x44, 0x45, 0x47, 0x48, 0x49
T_BEGIN_COLLECTION, T_END_COLLECTION, T_MEMBER_NAME = 0x34, 0x37, 0x4A

DEFAULT_PORT = 631
TIMEOUT_S = 60
PRINTER_STATES = {3: "idle", 4: "processing", 5: "stopped"}
JOB_STATES = {3: "pending", 4: "pending-held", 5: "processing", 6: "processing-stopped", 7: "canceled",
              8: "aborted", 9: "completed"}
PRINTER_ATTRIBUTES = ["printer-state", "printer-state-reasons", "printer-make-and-model", "media-supported",
                      "media-source-supported", "document-format-supported"]
STATUS_NAMES = {0x0400: "client-error-bad-request", 0x0401: "client-error-forbidden",
                0x0402: "client-error-not-authenticated", 0x0403: "client-error-not-authorized",
                0x0404: "client-error-not-possible", 0x0405: "client-error-timeout",
                0x0406: "client-error-not-found", 0x040A: "client-error-document-format-not-supported",
                0x040B: "client-error-attributes-or-values-not-supported", 0x0500: "server-error-internal-error",
                0x0501: "server-error-operation-not-supported", 0x0502: "server-error-service-unavailable",
                0x0504: "server-error-device-error", 0x0505: "server-error-temporary-error",
                0x0506: "server-error-not-accepting-jobs", 0x0507: "server-error-busy",
                0x0508: "server-error-job-canceled"}
_SIZE = re.compile(r"(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)mm")


class IPPError(RuntimeError):
    pass


@dataclass(frozen=True)
class Attr:
    """One attribute: value tag, name, and a value, a list of values (1setOf) or a dict (collection)."""
    name: str
    tag: int
    value: Any


@dataclass
class Message:
    version: tuple[int, int]
    code: int                       # operation id of a request, status code of a response
    request_id: int
    groups: dict[int, dict[str, list[Any]]]
    document: bytes = b""

    @property
    def ok(self) -> bool:
        return self.code < 0x0100

    def values(self, name: str) -> list[Any]:
        """All values of an attribute from the first group that has it (unsupported attributes excluded)."""
        for group, attrs in self.groups.items():
            if group != G_UNSUPPORTED and name in attrs:
                return attrs[name]
        return []

    def get(self, name: str, default: Any = None) -> Any:
        vals = self.values(name)
        return vals[0] if vals else default


# ------------------------------------------------------------------ encoding
def _head(tag: int, name: str, data: bytes) -> bytes:
    raw = name.encode("utf-8")
    return struct.pack(">BH", tag, len(raw)) + raw + struct.pack(">H", len(data)) + data


def _scalar(tag: int, value: Any) -> bytes:
    if tag in (T_INTEGER, T_ENUM):
        return struct.pack(">i", int(value))
    if tag == T_BOOLEAN:
        return b"\x01" if value else b"\x00"
    if tag == T_OCTETSTRING:
        return bytes(value)
    return str(value).encode("utf-8")


def _member_tag(value: Any) -> int:
    if isinstance(value, bool):
        return T_BOOLEAN
    return T_INTEGER if isinstance(value, int) else T_KEYWORD


def _collection(name: str, members: dict[str, Any]) -> bytes:
    out = bytearray(_head(T_BEGIN_COLLECTION, name, b""))
    for key, value in members.items():
        out += _head(T_MEMBER_NAME, "", key.encode("utf-8"))
        if isinstance(value, dict):
            out += _collection("", value)
        else:
            out += _head(_member_tag(value), "", _scalar(_member_tag(value), value))
    out += _head(T_END_COLLECTION, "", b"")
    return bytes(out)


def _attr_bytes(a: Attr) -> bytes:
    if a.tag == T_BEGIN_COLLECTION:
        return _collection(a.name, a.value)
    values = a.value if isinstance(a.value, list) else [a.value]
    return b"".join(_head(a.tag, a.name if i == 0 else "", _scalar(a.tag, v)) for i, v in enumerate(values))


def encode(operation: int, groups: list[tuple[int, list[Attr]]], document: bytes = b"",
           request_id: int | None = None) -> bytes:
    rid = request_id if request_id is not None else random.randint(1, 0x7FFFFFFF)
    out = bytearray(struct.pack(">BBHi", 1, 1, operation, rid))
    for group, attrs in groups:
        out.append(group)
        for a in attrs:
            out += _attr_bytes(a)
    out.append(G_END)
    return bytes(out) + document


def ipp_uri(uri: str) -> str:
    """The printer-uri attribute uses the ipp(s) scheme even when the address was typed as http(s)."""
    s = uri.strip()
    if s.startswith("http://"):
        return "ipp://" + s[len("http://"):]
    if s.startswith("https://"):
        return "ipps://" + s[len("https://"):]
    return s


def operation_attrs(printer_uri: str) -> list[Attr]:
    """The first attributes of a request; printers expect this order."""
    return [Attr("attributes-charset", T_CHARSET, "utf-8"),
            Attr("attributes-natural-language", T_LANGUAGE, "en"),
            Attr("printer-uri", T_URI, ipp_uri(printer_uri))]


def media_size_mm(name: str) -> tuple[float, float] | None:
    """Width and height in mm from a size name such as custom_100x150mm_100x150mm or iso_a4_210x297mm."""
    m = _SIZE.search(name or "")
    return (float(m[1]), float(m[2])) if m else None


def media_attrs(media: str | None, tray: str | None) -> list[Attr]:
    """`media` names the size. A tray needs media-col: media-source is a member of it, not a top-level attribute.
    Without a known size in the name the tray is not sent (the plain media keyword is used)."""
    size = media_size_mm(media or "")
    if not tray or tray == "auto" or size is None:
        return [Attr("media", T_KEYWORD, media)] if media else []
    col: dict[str, Any] = {"media-size": {"x-dimension": round(size[0] * 100),
                                          "y-dimension": round(size[1] * 100)},
                           "media-source": tray}
    return [Attr("media-col", T_BEGIN_COLLECTION, col)]


# ------------------------------------------------------------------ decoding
def _decode_value(tag: int, data: bytes) -> Any:
    if tag in (T_INTEGER, T_ENUM) and len(data) == 4:
        return struct.unpack(">i", data)[0]
    if tag == T_BOOLEAN:
        return data == b"\x01"
    if 0x10 <= tag <= 0x1F:                                    # out-of-band values carry no value
        return None
    if tag in (T_OCTETSTRING, 0x31, 0x32, 0x33):               # octets, dateTime, resolution, rangeOfInteger
        return data
    return data.decode("utf-8", "replace")


def decode(body: bytes) -> Message:
    """Parse a response (or a request: same header). Collections come back as dicts of their members."""
    if len(body) < 8 or body[0] != 1:
        raise IPPError(t("The printer did not answer with IPP."))
    code, rid = struct.unpack_from(">Hi", body, 2)
    msg = Message((body[0], body[1]), code, rid, {})
    pos, group, last, member = 8, None, "", ""
    open_collections: list[dict[str, Any]] = []        # innermost last
    try:
        while pos < len(body):
            tag = body[pos]
            pos += 1
            if tag == G_END:
                msg.document = body[pos:]
                break
            if tag <= 0x0F:                             # group delimiter
                group, last = tag, ""
                msg.groups.setdefault(tag, {})
                open_collections.clear()
                continue
            nlen = struct.unpack_from(">H", body, pos)[0]
            name = body[pos + 2:pos + 2 + nlen].decode("utf-8", "replace")
            pos += 2 + nlen
            vlen = struct.unpack_from(">H", body, pos)[0]
            data = body[pos + 2:pos + 2 + vlen]
            pos += 2 + vlen
            if len(data) != vlen:
                raise IPPError(t("The printer sent a malformed answer."))
            if tag == T_MEMBER_NAME:
                member = data.decode("utf-8", "replace")
                continue
            if tag == T_END_COLLECTION:
                if open_collections:
                    open_collections.pop()
                continue
            last = name or last                          # an empty name continues the previous attribute
            if tag == T_BEGIN_COLLECTION:
                value: Any = {}
                if open_collections:
                    open_collections[-1][member] = value
                elif group is not None:
                    msg.groups[group].setdefault(last, []).append(value)
                open_collections.append(value)
                continue
            if open_collections:                         # a member's value
                open_collections[-1][member] = _decode_value(tag, data)
            elif group is not None:
                msg.groups[group].setdefault(last, []).append(_decode_value(tag, data))
    except (struct.error, IndexError) as e:
        raise IPPError(t("The printer sent a malformed answer.")) from e
    return msg


# ------------------------------------------------------------------ transport
def http_url(uri: str) -> str:
    """ipp:// is HTTP and ipps:// is HTTPS, both on port 631 unless given; http(s) addresses are used as they are."""
    parts = urlsplit(uri.strip())
    scheme = {"ipp": "http", "ipps": "https"}.get(parts.scheme, parts.scheme)
    if scheme not in ("http", "https") or not parts.hostname:
        raise IPPError(t("The printer address must start with ipp:// (e.g. ipp://192.168.1.50/ipp/print)."))
    try:
        port = parts.port or DEFAULT_PORT
    except ValueError as e:
        raise IPPError(t("The printer address has an invalid port.")) from e
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return urlunsplit((scheme, f"{host}:{port}", parts.path or "/ipp/print", parts.query, ""))


async def exchange(uri: str, body: bytes, *, transport: httpx.AsyncBaseTransport | None = None) -> Message:
    """POST one IPP request and return the answer; IPPError for transport, HTTP and IPP status failures."""
    url = http_url(uri)
    try:
        # printers ship self-signed certificates on ipps://, so their certificate is not checked
        async with httpx.AsyncClient(transport=transport, timeout=TIMEOUT_S,
                                     verify=not url.startswith("https:")) as c:
            r = await c.post(url, content=body, headers={"Content-Type": "application/ipp"})
    except httpx.HTTPError as e:
        raise IPPError(t("The printer is not reachable: {error}", error=e)) from e
    if r.status_code in (401, 403):
        raise IPPError(t("The printer asks for a login. Use an address without one, e.g. a CUPS queue."))
    if r.status_code == 426:
        raise IPPError(t("The printer only accepts ipps:// (HTTPS)."))
    if r.status_code != 200:
        raise IPPError(t("The printer answered with HTTP {code}.", code=r.status_code))
    msg = decode(r.content)
    if not msg.ok:
        detail = msg.get("status-message") or STATUS_NAMES.get(msg.code, hex(msg.code))
        raise IPPError(t("The printer refused the request: {detail}", detail=detail))
    return msg


# ------------------------------------------------------------------ operations
async def print_job(uri: str, document: bytes, *, job_name: str, media: str | None, tray: str | None = None,
                    scaling: str = "fit", transport: httpx.AsyncBaseTransport | None = None) -> int:
    """Submit a PDF; returns the printer's job id. print-scaling "fit" shrinks what does not fit the medium."""
    op = [*operation_attrs(uri), Attr("requesting-user-name", T_NAME, "willhaben"),
          Attr("document-format", T_MIME, "application/pdf")]
    job = [Attr("job-name", T_NAME, job_name), Attr("copies", T_INTEGER, 1),
           Attr("print-scaling", T_KEYWORD, scaling), *media_attrs(media, tray)]
    msg = await exchange(uri, encode(PRINT_JOB, [(G_OPERATION, op), (G_JOB, job)], document), transport=transport)
    job_id = msg.get("job-id")
    if job_id is None:
        raise IPPError(t("The printer accepted the job but named no job id."))
    return int(job_id)


async def printer_attributes(uri: str, *, transport: httpx.AsyncBaseTransport | None = None) -> Message:
    op = [*operation_attrs(uri), Attr("requested-attributes", T_KEYWORD, PRINTER_ATTRIBUTES)]
    return await exchange(uri, encode(GET_PRINTER_ATTRIBUTES, [(G_OPERATION, op)]), transport=transport)


async def job_state(uri: str, job_id: int, *, transport: httpx.AsyncBaseTransport | None = None) -> str:
    op = [*operation_attrs(uri), Attr("job-id", T_INTEGER, job_id)]
    msg = await exchange(uri, encode(GET_JOB_ATTRIBUTES, [(G_OPERATION, op)]), transport=transport)
    return JOB_STATES.get(msg.get("job-state"), "unknown")
