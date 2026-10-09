import asyncio
import io
import struct
import time
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader, PdfWriter

from app.config import get_settings
from app.db import Conversation, Shipment, session, set_setting, utcnow
from app.services import dropoff, ipp, printing, shipping
from app.services.ipp import Attr

A4 = (210, 297)
LABEL = (100, 150)
GLS = (100, 200)


def box(mm):
    """A page box (x0, y0, x1, y1) in points for a size in mm."""
    return [0, 0] + [round(v * 72 / 25.4, 2) for v in mm]


class FakePrinter:
    """Answers IPP like a printer that accepts every job, and remembers what it was sent."""

    def __init__(self, *, status=0x0000, printer_state=3, job_id=42):
        self.requests: list[ipp.Message] = []
        self.status, self.printer_state, self.job_id = status, printer_state, job_id

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("content-type") != "application/ipp":
            return httpx.Response(415)
        msg = ipp.decode(request.content)
        self.requests.append(msg)
        if msg.code == ipp.GET_PRINTER_ATTRIBUTES:
            groups = [(ipp.G_PRINTER, [
                Attr("printer-state", ipp.T_ENUM, self.printer_state),
                Attr("printer-state-reasons", ipp.T_KEYWORD, ["none"]),
                Attr("media-supported", ipp.T_KEYWORD, ["iso_a4_210x297mm", "custom_100x150mm_100x150mm"]),
                Attr("media-source-supported", ipp.T_KEYWORD, ["auto", "tray-1"]),
                Attr("document-format-supported", ipp.T_MIME, ["application/pdf", "image/urf"])])]
        elif msg.code == ipp.GET_JOB_ATTRIBUTES:
            groups = [(ipp.G_JOB, [Attr("job-state", ipp.T_ENUM, 5)])]
        else:
            groups = [(ipp.G_OPERATION, [Attr("status-message", ipp.T_TEXT, "refused by test")]),
                      (ipp.G_JOB, [Attr("job-id", ipp.T_INTEGER, self.job_id), Attr("job-state", ipp.T_ENUM, 5)])]
        body = ipp.encode(self.status, groups, request_id=msg.request_id)
        return httpx.Response(200, content=body, headers={"Content-Type": "application/ipp"})


@pytest.fixture
def printer(monkeypatch):
    fake = FakePrinter()
    monkeypatch.setattr(printing, "_transport", httpx.MockTransport(fake))
    return fake


def a4_pdf(pages: int = 1) -> bytes:
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=box(A4)[2], height=box(A4)[3])
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


def configure(*, auto=True, label_uri="ipp://cups:631/printers/Brother_QL", normal_uri="ipp://192.168.1.50/ipp/print",
              **extra):
    form = {"auto": "1" if auto else "", "label_name": "QL", "label_uri": label_uri,
            "label_media": "custom_100x150mm_100x150mm", "label_tray": "auto",
            "normal_name": "A4", "normal_uri": normal_uri, "normal_media": "iso_a4_210x297mm", "normal_tray": "tray-1",
            "carrier_post": "normal", "carrier_gls": "label", "carrier_hermes": "label", "carrier_dpd": "label"}
    form.update(extra)
    printing.save(form)


def label_file(name="s1.pdf"):
    d = get_settings().data_dir / "labels"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(a4_pdf())


def make_shipment(status="labelled", option="gls:S:shop", label_path="s1.pdf") -> int:
    with session() as db:
        if db.get(Conversation, "c1") is None:
            db.add(Conversation(id="c1", ad_id="77", ad_title="Buch", partner_name="Max", role="seller"))
        s = Shipment(conversation_id="c1", status=status, option=option, label_path=label_path,
                     recipient={"name": "Max Muster", "street": "Hauptstraße 12", "zip": "1150", "city": "Wien"},
                     parcel={"length_cm": 30, "width_cm": 20, "height_cm": 5, "weight_kg": 1})
        db.add(s)
        db.commit()
        db.refresh(s)
        return s.id


# ------------------------------------------------------------------ IPP wire format
def test_ipp_round_trip_keeps_groups_values_collections_and_document():
    op = [*ipp.operation_attrs("ipp://printer.local/ipp/print"), Attr("document-format", ipp.T_MIME, "application/pdf")]
    job = [Attr("copies", ipp.T_INTEGER, 2), Attr("print-scaling", ipp.T_KEYWORD, "fit"),
           Attr("media-col", ipp.T_BEGIN_COLLECTION, {"media-size": {"x-dimension": 10000, "y-dimension": 15000},
                                                       "media-source": "tray-1"}),
           Attr("sides", ipp.T_KEYWORD, ["one-sided", "two-sided-long-edge"])]
    body = ipp.encode(ipp.PRINT_JOB, [(ipp.G_OPERATION, op), (ipp.G_JOB, job)], b"%PDF-1.4 test", request_id=7)
    assert body[:2] == b"\x01\x01" and struct.unpack(">H", body[2:4])[0] == ipp.PRINT_JOB
    msg = ipp.decode(body)
    assert msg.code == ipp.PRINT_JOB and msg.request_id == 7 and msg.document == b"%PDF-1.4 test"
    assert msg.get("printer-uri") == "ipp://printer.local/ipp/print"
    assert msg.get("document-format") == "application/pdf"
    assert msg.get("copies") == 2 and msg.get("print-scaling") == "fit"
    assert msg.values("sides") == ["one-sided", "two-sided-long-edge"]
    assert msg.get("media-col") == {"media-size": {"x-dimension": 10000, "y-dimension": 15000}, "media-source": "tray-1"}
    assert "media-source" not in msg.groups[ipp.G_JOB]                       # members stay inside the collection


def test_ipp_addresses_media_names_and_tray_handling():
    assert ipp.http_url("ipp://192.168.1.50/ipp/print") == "http://192.168.1.50:631/ipp/print"
    assert ipp.http_url("ipps://printer:6631/x?y=1") == "https://printer:6631/x?y=1"
    assert ipp.http_url("ipp://cups:631/printers/Brother_QL") == "http://cups:631/printers/Brother_QL"
    assert ipp.http_url("ipp://[fd00::1]") == "http://[fd00::1]:631/ipp/print"
    with pytest.raises(ipp.IPPError):
        ipp.http_url("smb://nope/printer")
    assert ipp.media_size_mm("custom_100x150mm_100x150mm") == (100, 150)
    assert ipp.media_size_mm("iso_a4_210x297mm") == (210, 297)
    assert ipp.media_size_mm("na_letter_8.5x11in") is None
    col = ipp.media_attrs("iso_a4_210x297mm", "tray-2")
    assert col[0].tag == ipp.T_BEGIN_COLLECTION
    assert col[0].value == {"media-size": {"x-dimension": 21000, "y-dimension": 29700}, "media-source": "tray-2"}
    assert ipp.media_attrs("iso_a4_210x297mm", "auto")[0].name == "media"
    assert ipp.media_attrs("iso_a4_210x297mm", None)[0].name == "media"
    assert ipp.media_attrs("unknown_size", "tray-2")[0].name == "media"       # no size in the name: keyword only


async def test_print_job_request_reaches_the_printer_with_media_tray_and_pdf(printer):
    job = await ipp.print_job("ipp://192.168.1.50/ipp/print", b"%PDF-1.4 x", job_name="job",
                              media="iso_a4_210x297mm", tray="tray-1", transport=httpx.MockTransport(printer))
    assert job == 42
    req = printer.requests[-1]
    assert req.code == ipp.PRINT_JOB and req.document == b"%PDF-1.4 x"
    assert req.get("document-format") == "application/pdf" and req.get("print-scaling") == "fit"
    assert req.get("printer-uri") == "ipp://192.168.1.50/ipp/print"
    assert req.get("copies") == 1 and req.get("job-name") == "job"
    assert req.get("media-col") == {"media-size": {"x-dimension": 21000, "y-dimension": 29700}, "media-source": "tray-1"}
    assert req.get("media") is None                                          # the collection replaces the keyword


async def test_refusals_and_transport_failures_become_ipp_errors():
    with pytest.raises(ipp.IPPError, match="refused"):
        await ipp.print_job("ipp://p/ipp/print", b"%PDF", job_name="x", media=None,
                            transport=httpx.MockTransport(FakePrinter(status=0x0406)))

    async def not_found(request):
        return httpx.Response(404, text="nope")
    with pytest.raises(ipp.IPPError, match="HTTP 404"):
        await ipp.printer_attributes("ipp://p/ipp/print", transport=httpx.MockTransport(not_found))

    def unreachable(request):
        raise httpx.ConnectError("no route")
    with pytest.raises(ipp.IPPError, match="not reachable"):
        await ipp.printer_attributes("ipp://p/ipp/print", transport=httpx.MockTransport(unreachable))


async def test_printer_attributes_and_job_state_on_a_fake_printer(printer):
    transport = httpx.MockTransport(printer)
    msg = await ipp.printer_attributes("ipp://p/ipp/print", transport=transport)
    assert msg.get("printer-state") == 3 and "iso_a4_210x297mm" in msg.values("media-supported")
    assert await ipp.job_state("ipp://p/ipp/print", 42, transport=transport) == "processing"


# ------------------------------------------------------------------ settings
def test_defaults_enabled_and_saving_through_the_settings_route():
    cfg = printing.config()
    assert cfg["auto"] is False
    assert cfg["by_carrier"] == {"post": "normal", "gls": "label", "hermes": "label", "dpd": "label"}
    assert cfg["printers"]["label"]["carriers"] == {} and cfg["printers"]["normal"]["media"] == "iso_a4_210x297mm"
    assert printing.enabled() is False

    from app.main import app
    with TestClient(app) as c:
        r = c.post("/settings/printers", data={
            "auto": "1", "label_uri": "ipp://cups:631/printers/Brother_QL",
            "label_media": "other", "label_media_other": "custom_100x200mm_100x200mm", "label_tray": "auto",
            "normal_uri": "", "normal_media": "iso_a4_210x297mm", "normal_tray": "auto",
            "carrier_post": "normal", "carrier_gls": "label", "carrier_hermes": "label", "carrier_dpd": "none",
            "crop_gls_left": "0.05", "crop_gls_top": "0.1", "crop_gls_width": "0.9", "crop_gls_height": "0.4",
            "label_carrier_media_hermes": "other", "label_carrier_media_hermes_other": "custom_100x150mm_100x150mm"},
            follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].endswith("#printers") and "saved" in r.headers["location"]
        page = c.get("/settings").text
        assert 'id="printers"' in page and 'name="label_uri"' in page and "Test page" in page
    cfg = printing.config()
    assert cfg["auto"] is True and cfg["printers"]["label"]["media"] == "custom_100x200mm_100x200mm"
    assert cfg["by_carrier"]["dpd"] == "none"
    assert cfg["printers"]["label"]["carriers"] == {
        "gls": {"crop": {"left": 0.05, "top": 0.1, "width": 0.9, "height": 0.4}},
        "hermes": {"media": "custom_100x150mm_100x150mm"}}
    assert printing.enabled() is False                    # post goes to the normal printer, which has no address
    configure(normal_uri="ipp://192.168.1.50/ipp/print", carrier_post="none", carrier_gls="label",
              carrier_hermes="none", carrier_dpd="none")
    assert printing.enabled() is True                     # only the label printer is used, and it has an address


def test_invalid_settings_are_refused_and_nothing_changes():
    configure()
    before = printing.config()
    with pytest.raises(printing.PrintError, match="ipp://"):
        printing.save({"label_uri": "smb://x", "normal_uri": ""})
    with pytest.raises(printing.PrintError, match="fractions"):
        printing.save({"label_uri": "", "normal_uri": "", "crop_post_left": "0.5", "crop_post_top": "0",
                       "crop_post_width": "0.8", "crop_post_height": "0.2"})
    with pytest.raises(printing.PrintError, match="media or tray"):
        printing.save({"label_uri": "", "normal_uri": "", "label_media": "bad name!"})
    assert printing.config() == before


def test_broken_stored_json_falls_back_to_defaults():
    set_setting("printers", "{not json")
    cfg = printing.config()
    assert cfg["auto"] is False and cfg["printers"]["label"]["media"] == "custom_100x150mm_100x150mm"


# ------------------------------------------------------------------ PDF preparation
def test_label_pdf_crops_turns_and_fits_onto_the_medium():
    # the top 30 % of an A4 page is a landscape strip; on 100 x 150 mm portrait media it is turned and fitted
    out = printing.label_pdf(a4_pdf(), "custom_100x150mm_100x150mm",
                             {"left": 0.0, "top": 0.0, "width": 1.0, "height": 0.3})
    reader = PdfReader(io.BytesIO(out))
    assert len(reader.pages) == 1
    page = reader.pages[0]
    assert [round(float(v), 2) for v in page.mediabox] == box(LABEL)
    assert [round(float(v), 2) for v in page.cropbox] == box(LABEL)
    assert int(page.get("/Rotate", 0)) == 0
    content = page.get_contents().get_data()
    assert content.startswith(b"q ") and b"re W n" in content and b" cm" in content


def test_label_pdf_without_crop_takes_the_first_page_only():
    out = printing.label_pdf(a4_pdf(pages=3), "custom_100x150mm_100x150mm")
    reader = PdfReader(io.BytesIO(out))
    assert len(reader.pages) == 1
    assert [round(float(v), 2) for v in reader.pages[0].mediabox] == box(LABEL)


def test_label_pdf_refuses_garbage():
    with pytest.raises(printing.PrintError, match="readable PDF"):
        printing.label_pdf(b"not a pdf", "custom_100x150mm_100x150mm")


def test_test_page_has_the_size_of_the_medium():
    reader = PdfReader(io.BytesIO(printing.test_page_pdf("custom_100x150mm_100x150mm")))
    assert [round(float(v), 2) for v in reader.pages[0].mediabox] == box(LABEL)
    assert "willhaben Agent test page" in reader.pages[0].extract_text()


# ------------------------------------------------------------------ printing
async def test_test_page_and_check_use_the_saved_printers(printer):
    configure()
    assert await printing.test_page("normal") == "42"
    req = printer.requests[-1]
    assert req.document.startswith(b"%PDF") and req.get("printer-uri") == "ipp://192.168.1.50/ipp/print"
    assert req.get("media-col")["media-source"] == "tray-1"
    report = await printing.check("label")
    assert "idle" in report and "custom_100x150mm_100x150mm" in report and "tray-1" in report and "PDF: yes" in report
    configure(label_uri="")
    with pytest.raises(printing.PrintError, match="no address"):
        await printing.test_page("label")


async def test_print_label_moves_labelled_to_printed_on_the_label_printer(printer):
    configure()
    label_file()
    sid = make_shipment()
    assert await printing.print_label(sid) == "42"
    s = shipping.get(sid)
    assert s.status == "printed" and s.printed_at is not None and s.error is None
    assert s.print_job == "ipp://cups:631/printers/Brother_QL#42"               # GLS goes to the label printer
    req = printer.requests[-1]
    assert req.get("printer-uri") == "ipp://cups:631/printers/Brother_QL"
    assert req.get("media") == "custom_100x200mm_100x200mm"               # GLS: 100 x 200 mm while the printer says 100 x 150
    prepared = PdfReader(io.BytesIO(req.document)).pages[0]
    assert [round(float(v), 2) for v in prepared.mediabox] == box(GLS)


async def test_print_label_keeps_status_and_sends_post_to_the_normal_printer(printer):
    configure()
    label_file()
    sid = make_shipment(status="sent", option="post:Päckchen S:home")
    await printing.print_label(sid)
    s = shipping.get(sid)
    assert s.status == "sent" and s.print_job.startswith("ipp://192.168.1.50/ipp/print#")
    req = printer.requests[-1]
    assert req.get("print-scaling") == "fit" and req.get("media-col")["media-source"] == "tray-1"
    assert req.document.startswith(b"%PDF")                                      # normal printers: PDF as it is


async def test_print_label_errors_and_explicit_choice(printer):
    configure(carrier_post="none")
    label_file()
    with pytest.raises(printing.PrintError, match="No printer is set"):
        await printing.print_label(make_shipment(option="post:Päckchen S:home"))
    sid = make_shipment(option="post:Päckchen S:home")
    await printing.print_label(sid, "normal")                                    # explicit choice ignores the carrier
    assert shipping.get(sid).print_job.startswith("ipp://192.168.1.50")
    with pytest.raises(printing.PrintError, match="No label"):
        await printing.print_label(make_shipment(label_path=None))
    with pytest.raises(printing.PrintError, match="missing"):
        await printing.print_label(make_shipment(label_path="gone.pdf"))
    with pytest.raises(printing.PrintError, match="not found"):
        await printing.print_label(999)


async def test_refused_print_is_stored_on_the_shipment_and_the_status_stays(monkeypatch):
    monkeypatch.setattr(printing, "_transport", httpx.MockTransport(FakePrinter(status=0x0500)))
    configure()
    label_file()
    sid = make_shipment()
    with pytest.raises(printing.PrintError, match="refused"):
        await printing.print_label(sid)
    s = shipping.get(sid)
    assert s.status == "labelled" and s.printed_at is None and "refused" in (s.error or "")


# ------------------------------------------------------------------ chat page and routes
def chat_with_label(monkeypatch, *, status="labelled", printed=False):
    async def nearest(*a, **k):
        return {}
    monkeypatch.setattr(dropoff, "nearest", nearest)
    configure()
    label_file()
    sid = make_shipment(status=status)
    if printed:
        shipping.update(sid, printed_at=utcnow(), print_job="ipp://cups:631/printers/Brother_QL#7")
    return sid


def test_chat_page_shows_the_print_buttons_and_the_print_line(monkeypatch):
    sid = chat_with_label(monkeypatch)
    from app.main import app
    with TestClient(app) as c:
        r = c.get("/chats/c1")
        assert r.status_code == 200
        assert f'action="/shipments/{sid}/print"' in r.text
        assert 'name="which" value="auto"' in r.text and 'name="which" value="normal"' in r.text   # GLS: label is the default
        assert "(job 7)" not in r.text
        shipping.update(sid, printed_at=utcnow(), print_job="ipp://cups:631/printers/Brother_QL#7")
        assert "(job 7)" in c.get("/chats/c1").text


def test_print_route_prints_and_redirects_to_the_chat(monkeypatch, printer):
    sid = chat_with_label(monkeypatch)
    from app.main import app
    with TestClient(app) as c:
        r = c.post(f"/shipments/{sid}/print", data={"which": "auto"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/chats/c1?msg=")
        assert r.headers["location"].endswith("#shipping")
        assert shipping.get(sid).status == "printed"
        assert c.post(f"/shipments/{sid}/print", data={"which": "bogus"}, follow_redirects=False).status_code == 400
        assert c.post("/shipments/999/print", data={"which": "auto"}, follow_redirects=False).status_code == 404


def test_test_and_check_routes_redirect_back_to_the_printers(printer):
    configure()
    from app.main import app
    with TestClient(app) as c:
        r = c.post("/settings/printers/test/label", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].endswith("#printers") and "job" in r.headers["location"]
        r = c.post("/settings/printers/check/normal", follow_redirects=False)
        assert r.status_code == 303 and "PDF" in r.headers["location"]


# ------------------------------------------------------------------ per-carrier media and job state
def test_media_per_carrier_defaults_and_overrides():
    configure()
    cfg = printing.config()
    assert printing.media_for(cfg, "gls") == "custom_100x200mm_100x200mm"    # GLS default on a 100 x 150 printer
    assert printing.media_for(cfg, "hermes") == "custom_100x150mm_100x150mm"
    assert printing.media_for(cfg, "dpd") == "custom_100x150mm_100x150mm"
    configure(label_carrier_media_gls="custom_100x150mm_100x150mm")          # an explicit choice wins
    assert printing.media_for(printing.config(), "gls") == "custom_100x150mm_100x150mm"
    configure(label_media="custom_100x200mm_100x200mm")                      # printer on 100 x 200: GLS follows
    assert printing.media_for(printing.config(), "gls") == "custom_100x200mm_100x200mm"
    assert printing.media_for(printing.config(), "hermes") == "custom_100x200mm_100x200mm"
    configure(label_carrier_media_dpd="other", label_carrier_media_dpd_other="custom_50x80mm_50x80mm")
    assert printing.media_for(printing.config(), "dpd") == "custom_50x80mm_50x80mm"
    with pytest.raises(printing.PrintError, match="media or tray"):
        configure(label_carrier_media_dpd="other", label_carrier_media_dpd_other="bad name!")


async def test_print_label_uses_the_carrier_medium_for_pdf_and_ipp(printer):
    configure(label_carrier_media_hermes="other", label_carrier_media_hermes_other="custom_100x150mm_100x150mm")
    label_file()
    await printing.print_label(make_shipment(option="gls:S:shop"))            # GLS: 100 x 200
    assert printer.requests[-1].get("media") == "custom_100x200mm_100x200mm"
    assert printer.requests[-1].get("print-scaling") == "fit"
    await printing.print_label(make_shipment(option="hermes:M:home"))         # Hermes: explicit 100 x 150
    req = printer.requests[-1]
    assert req.get("media") == "custom_100x150mm_100x150mm"
    assert [round(float(v), 2) for v in PdfReader(io.BytesIO(req.document)).pages[0].mediabox] == box(LABEL)
    await printing.print_label(make_shipment(option="dpd:M:home"))            # DPD: the printer's medium
    assert printer.requests[-1].get("media") == "custom_100x150mm_100x150mm"


async def test_job_state_is_asked_with_a_timeout_and_errors_become_unknown(printer, monkeypatch):
    assert await printing.job_state("ipp://p/ipp/print#42") == "processing"

    def unreachable(request):
        raise httpx.ConnectError("no route")
    monkeypatch.setattr(printing, "_transport", httpx.MockTransport(unreachable))
    assert await printing.job_state("ipp://p/ipp/print#42") == "unknown"


async def test_cached_job_state_answers_at_once_and_refreshes_in_the_background(printer):
    uri = "ipp://cups:631/printers/Brother_QL#42"
    assert printing.cached_job_state(None, utcnow()) is None                 # not printed
    assert printing.cached_job_state(uri, utcnow() - timedelta(days=2)) is None   # older than a day
    assert printing.cached_job_state(uri, utcnow()) == "checking"            # first answer comes from the background
    for _ in range(50):
        await asyncio.sleep(0.01)
        if uri in printing._job_states:
            break
    assert printing.cached_job_state(uri, utcnow()) == "in progress"           # the printer said "processing"


def test_job_state_on_the_chat_page(monkeypatch, printer):
    sid = chat_with_label(monkeypatch)
    shipping.update(sid, printed_at=utcnow(), print_job="ipp://cups:631/printers/Brother_QL#42")
    from app.main import app
    with TestClient(app) as c:
        c.get("/chats/c1")                                                   # starts the background question
        for _ in range(100):
            if "in progress" in c.get("/chats/c1").text:
                break
            time.sleep(0.02)
        assert "job state: in progress" in c.get("/chats/c1").text
