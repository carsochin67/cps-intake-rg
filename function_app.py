"""
CPS Digital Intake — Azure Function (Table Storage version).
All data lives in Azure: each intake is written to Azure Table Storage in the
same storage account the Function app already uses, via the app's managed
identity. No SharePoint, no Microsoft Graph, no Entra directory-admin.

App settings:
    STORAGE_TABLE_ENDPOINT = https://<youraccount>.table.core.windows.net
    TABLE_NAME             = CPSIntakes
    TEMPLATE_PATH          = LDSS-2221A-Fillable-Dropdowns-V4.pdf

NOTE ON PHI: LDSS-2221A data is PHI. Production needs a Microsoft BAA,
encryption in transit/at rest, Entra auth, and audit logging.
"""
from __future__ import annotations
import io
import os
import json
import uuid
import logging
import datetime as dt
import base64

import azure.functions as func
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject



app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

TEMPLATE_PATH = os.getenv("TEMPLATE_PATH", "LDSS-2221A-Fillable-Dropdowns-V4.pdf")
TABLE_ENDPOINT = os.getenv("STORAGE_TABLE_ENDPOINT", "")
TABLE_NAME = os.getenv("TABLE_NAME", "CPSIntakes")
# Blob container that keeps a permanent copy of every filled PDF, for records.
BLOB_ENDPOINT = os.getenv("STORAGE_BLOB_ENDPOINT", "")
PDF_CONTAINER = os.getenv("PDF_CONTAINER", "cps-pdfs")

REQUIRED = ("PatientMRN",)

def _template_bytes() -> bytes:
    try:
        from template_data import TEMPLATE_B64
        return base64.b64decode(TEMPLATE_B64)
    except Exception:
        with open(TEMPLATE_PATH, "rb") as f:
            return f.read()

def fill_pdf(payload: dict) -> bytes:
    """Fill the AcroForm, then FLATTEN it so the archived / printable copy has
    no interactive widgets: no grey combo-box dropdown arrows, no clickable
    fields. Values and checkmarks are baked into the page content.

    Pipeline:
      1. pypdf fills text/choice fields (str) and checkboxes ("/On" state).
      2. Strip pypdf's font-broken /AP from Tx/Ch widgets so qpdf rebuilds them.
      3. pikepdf: inject a real Helvetica into AcroForm.DR (the template's field
         /DA references /Helvetica but the form has no /DR font resource), set
         NeedAppearances, regenerate appearance streams, then flatten.
    """
    reader = PdfReader(io.BytesIO(_template_bytes()))
    writer = PdfWriter()
    writer.append(reader)

    text_choice = {}
    checkboxes = {}
    fields = reader.get_fields() or {}
    for key, val in payload.items():
        meta = fields.get(key)
        if isinstance(val, bool):
            if val:
                checkboxes[key] = _checkbox_on_state(meta)
        else:
            text_choice[key] = str(val)

    for page in writer.pages:
        if text_choice:
            writer.update_page_form_field_values(page, text_choice, auto_regenerate=False)
        if checkboxes:
            writer.update_page_form_field_values(page, checkboxes, auto_regenerate=False)

    # Remove pypdf's generated /AP from text/choice widgets. Those appearance
    # streams reference /Helvetica, which is unresolvable in this template, so
    # they'd render blank; deleting them lets qpdf rebuild clean appearances.
    for page in writer.pages:
        annots = page.get("/Annots")
        if not annots:
            continue
        for a in annots.get_object():
            o = a.get_object()
            if (o.get("/Subtype") == "/Widget"
                    and o.get("/FT") in ("/Tx", "/Ch")
                    and "/AP" in o):
                del o[NameObject("/AP")]

    buf = io.BytesIO()
    writer.write(buf)
    buf.seek(0)

    return _flatten_pdf(buf)


def _flatten_pdf(buf: io.BytesIO) -> bytes:
    """Regenerate appearances and flatten all annotations with pikepdf."""
    import pikepdf
    from pikepdf import Name, Dictionary

    pdf = pikepdf.open(buf)
    acro = pdf.Root.AcroForm

    # The template's fields use /DA "/Helvetica 8 Tf" but the AcroForm has no
    # /DR font resource, so appearance generation can't resolve the font.
    # Inject a standard Type1 Helvetica so qpdf can draw the text.
    helv = pdf.make_indirect(Dictionary(
        Type=Name.Font, Subtype=Name.Type1,
        BaseFont=Name.Helvetica, Encoding=Name.WinAnsiEncoding,
    ))
    if "/DR" not in acro:
        acro.DR = Dictionary()
    if "/Font" not in acro.DR:
        acro.DR.Font = Dictionary()
    acro.DR.Font.Helvetica = helv
    acro.NeedAppearances = True

    pdf.generate_appearance_streams()
    pdf.flatten_annotations()

    out = io.BytesIO()
    pdf.save(out)
    return out.getvalue()


def _checkbox_on_state(meta) -> str:
    # Return the on-state WITH a leading slash (e.g. "/Yes"). The slash is
    # required: it makes pypdf set the widget's /AS, without which a checked
    # box flattens to an empty box (no visible checkmark).
    try:
        states = meta.get("/_States_") if meta else None
        if states:
            for s in states:
                if s not in ("/Off", "Off"):
                    return "/" + s.lstrip("/")
    except Exception:
        pass
    return "/Yes"


def _table_client():
    from azure.data.tables import TableServiceClient
    from azure.identity import DefaultAzureCredential

    if not TABLE_ENDPOINT:
        raise RuntimeError("STORAGE_TABLE_ENDPOINT app setting is not set.")
    svc = TableServiceClient(endpoint=TABLE_ENDPOINT, credential=DefaultAzureCredential())
    svc.create_table_if_not_exists(TABLE_NAME)
    return svc.get_table_client(TABLE_NAME)


def write_table_item(payload: dict) -> str:
    now = dt.datetime.utcnow()
    intake_id = now.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]

    entity = {
        "PartitionKey": now.strftime("%Y-%m-%d"),
        "RowKey": intake_id,
        "SubmittedAtUtc": now.isoformat(timespec="seconds") + "Z",
    }
    for k, v in payload.items():
        entity[k] = v if isinstance(v, (str, bool, int, float)) else json.dumps(v)

    _table_client().create_entity(entity=entity)
    return intake_id


def archive_pdf(intake_id: str, partition: str, pdf_bytes: bytes) -> str:
    """Save a copy of the filled PDF to Blob Storage for record keeping.
    Stored as  <container>/<YYYY-MM-DD>/<intakeId>.pdf . Returns the blob path."""
    from azure.storage.blob import BlobServiceClient, ContentSettings
    from azure.identity import DefaultAzureCredential

    if not BLOB_ENDPOINT:
        raise RuntimeError("STORAGE_BLOB_ENDPOINT app setting is not set.")
    svc = BlobServiceClient(account_url=BLOB_ENDPOINT, credential=DefaultAzureCredential())
    container = svc.get_container_client(PDF_CONTAINER)
    try:
        container.create_container()
    except Exception:
        pass  # already exists
    blob_name = f"{partition}/{intake_id}.pdf"
    container.upload_blob(
        name=blob_name, data=pdf_bytes, overwrite=True,
        content_settings=ContentSettings(content_type="application/pdf"),
    )
    return blob_name


def read_table_items(limit: int = 200) -> list[dict]:
    rows = list(_table_client().list_entities())
    rows.sort(key=lambda e: e.get("SubmittedAtUtc", ""), reverse=True)
    return [dict(r) for r in rows[:limit]]


import re
# Intake IDs look like 20260710T170658-d85c0cc8. Validate strictly before
# using the value to build a blob path, so a request can never reach outside
# the container (path traversal) or probe arbitrary blob names.
_INTAKE_ID_RE = re.compile(r"^\d{8}T\d{6}-[0-9a-f]{8}$")


def read_pdf_blob(intake_id: str) -> bytes | None:
    """Fetch the archived PDF for one intake from Blob Storage.
    The blob path is derived from the intake id: <YYYY-MM-DD>/<intakeId>.pdf .
    Returns the PDF bytes, or None if it does not exist."""
    from azure.storage.blob import BlobServiceClient
    from azure.identity import DefaultAzureCredential
    from azure.core.exceptions import ResourceNotFoundError

    if not BLOB_ENDPOINT:
        raise RuntimeError("STORAGE_BLOB_ENDPOINT app setting is not set.")
    partition = intake_id[0:4] + "-" + intake_id[4:6] + "-" + intake_id[6:8]
    blob_name = f"{partition}/{intake_id}.pdf"
    svc = BlobServiceClient(account_url=BLOB_ENDPOINT, credential=DefaultAzureCredential())
    blob = svc.get_blob_client(container=PDF_CONTAINER, blob=blob_name)
    try:
        return blob.download_blob().readall()
    except ResourceNotFoundError:
        return None


@app.route(route="submit", methods=["POST"])
def submit(req: func.HttpRequest) -> func.HttpResponse:
    try:
        payload = req.get_json()
    except ValueError:
        return func.HttpResponse("Body must be JSON.", status_code=400)

    missing = [k for k in REQUIRED if not payload.get(k)]
    if missing:
        return func.HttpResponse(
            json.dumps({"error": "missing required fields", "fields": missing}),
            status_code=422, mimetype="application/json",
        )

    try:
        intake_id = write_table_item(payload)
    except Exception:
        logging.exception("Table Storage write failed")
        return func.HttpResponse("Could not save intake.", status_code=502)

    try:
        pdf_bytes = fill_pdf(payload)
    except Exception as e:
        logging.exception("PDF fill failed")
        return func.HttpResponse(
            json.dumps({"saved": True, "intakeId": intake_id, "pdf": False, "error": str(e)}),
            status_code=207, mimetype="application/json",
        )

    # Keep the only copy of the filled PDF in Blob Storage for records.
    # The PDF is NOT returned to the reporter: it stays in the backend so no
    # PHI copy lands on the social worker's device. A blob failure is logged
    # and surfaced in the response rather than raised.
    pdf_blob = ""
    partition = intake_id[0:4] + "-" + intake_id[4:6] + "-" + intake_id[6:8]
    try:
        pdf_blob = archive_pdf(intake_id, partition, pdf_bytes)
        try:
            from azure.data.tables import UpdateMode
            _table_client().update_entity(
                {"PartitionKey": partition, "RowKey": intake_id,
                 "PdfBlobPath": pdf_blob, "PdfArchived": True},
                mode=UpdateMode.MERGE,
            )
        except Exception:
            logging.exception("PDF-path table update failed (PDF still archived)")
    except Exception:
        logging.exception("PDF archive to Blob Storage failed")

    # Return only a confirmation. The completed form itself never leaves the
    # backend; authorized staff view it later via the intakes records page.
    return func.HttpResponse(
        json.dumps({
            "saved": True,
            "intakeId": intake_id,
            "pdfArchived": bool(pdf_blob),
            "pdfBlob": pdf_blob,
        }),
        status_code=200,
        mimetype="application/json",
    )


@app.route(route="intakes", methods=["GET"])
def intakes(req: func.HttpRequest) -> func.HttpResponse:
    try:
        rows = read_table_items()
    except Exception:
        logging.exception("Table Storage read failed")
        return func.HttpResponse("Could not read intakes.", status_code=502)
    return func.HttpResponse(
        json.dumps({"count": len(rows), "intakes": rows}, default=str),
        status_code=200, mimetype="application/json",
    )


@app.route(route="pdf/{intakeId}", methods=["GET"])
def pdf(req: func.HttpRequest) -> func.HttpResponse:
    intake_id = req.route_params.get("intakeId", "")
    if not _INTAKE_ID_RE.match(intake_id):
        return func.HttpResponse("Invalid intake id.", status_code=400)
    try:
        pdf_bytes = read_pdf_blob(intake_id)
    except Exception:
        logging.exception("PDF blob read failed")
        return func.HttpResponse("Could not read PDF.", status_code=502)
    if pdf_bytes is None:
        return func.HttpResponse("No archived PDF for that intake.", status_code=404)
    return func.HttpResponse(
        pdf_bytes,
        status_code=200,
        mimetype="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{intake_id}.pdf"',
        },
    )


@app.route(route="health", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def health(req: func.HttpRequest) -> func.HttpResponse:
    try:
        ok = len(_template_bytes()) > 0
    except Exception:
        ok = False
    return func.HttpResponse(
        json.dumps({
            "status": "ok" if ok else "degraded",
            "template": ok,
            "tableEndpointSet": bool(TABLE_ENDPOINT),
            "table": TABLE_NAME,
            "blobEndpointSet": bool(BLOB_ENDPOINT),
            "pdfContainer": PDF_CONTAINER,
        }),
        status_code=200 if ok else 503, mimetype="application/json",
    )