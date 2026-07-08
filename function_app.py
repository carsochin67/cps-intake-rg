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



app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

TEMPLATE_PATH = os.getenv("TEMPLATE_PATH", "LDSS-2221A-Fillable-Dropdowns-V4.pdf")
TABLE_ENDPOINT = os.getenv("STORAGE_TABLE_ENDPOINT", "")
TABLE_NAME = os.getenv("TABLE_NAME", "CPSIntakes")

REQUIRED = ("PatientMRN",)

def _template_bytes() -> bytes:
    try:
        from template_data import TEMPLATE_B64
        return base64.b64decode(TEMPLATE_B64)
    except Exception:
        with open(TEMPLATE_PATH, "rb") as f:
            return f.read()

def fill_pdf(payload: dict) -> bytes:
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

    try:
        writer.set_need_appearances_writer(True)
    except Exception:
        pass

    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _checkbox_on_state(meta) -> str:
    try:
        states = meta.get("/_States_") if meta else None
        if states:
            for s in states:
                if s not in ("/Off", "Off"):
                    return s.lstrip("/")
    except Exception:
        pass
    return "Yes"


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


def read_table_items(limit: int = 200) -> list[dict]:
    rows = list(_table_client().list_entities())
    rows.sort(key=lambda e: e.get("SubmittedAtUtc", ""), reverse=True)
    return [dict(r) for r in rows[:limit]]


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

    return func.HttpResponse(
        pdf_bytes,
        status_code=200,
        mimetype="application/pdf",
        headers={
            "Content-Disposition": 'inline; filename="LDSS-2221A-completed.pdf"',
            "X-Intake-Id": intake_id,
        },
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
        }),
        status_code=200 if ok else 503, mimetype="application/json",
    )