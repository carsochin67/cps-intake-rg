"""
Azure Function (Python v2 model) — Deterministic Word content-control extractor.

Reads the named content controls (<w:sdt> tags) stored inside a .docx and
returns them as clean {tag: value} JSON. No OCR, no ML, no guessing — the
result is identical every time for the same file.

HIPAA notes:
  * Runs inside YOUR Azure subscription, covered by Microsoft's BAA.
  * Does NOT log field values (PHI). Only counts are logged.
  * Stateless: nothing is written to disk or storage.

Accepts EITHER:
  * raw .docx bytes as the request body, OR
  * JSON body: {"fileContentBase64": "<base64 of the .docx>"}
    (this is what Power Automate's "Get file content" gives you via $content)

Returns:
  { "fieldCount": 191, "fields": { "PatientMRN": "1234567890", ... } }
"""

import base64
import io
import json
import logging
import zipfile
import xml.etree.ElementTree as ET

import azure.functions as func

app = func.FunctionApp()

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _q(tag: str) -> str:
    return f"{{{W}}}{tag}"


# A bare unchecked-checkbox glyph means "not checked". Anything else (e.g. "X")
# means checked. Normalize so downstream gets true booleans for checkbox fields.
_UNCHECKED_GLYPHS = {"☐", ""}  # ☐ or empty


def _normalize(value: str) -> str:
    v = (value or "").strip()
    # Strip a leading/trailing unchecked-box glyph that some controls carry.
    v = v.replace("☐", "").strip()
    return v


def extract_content_controls(docx_bytes: bytes) -> dict:
    """Return {tag: value} for every <w:sdt> content control in the document."""
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as z:
        xml = z.read("word/document.xml")
    root = ET.fromstring(xml)

    results: dict[str, str] = {}
    dup_counts: dict[str, int] = {}

    for sdt in root.iter(_q("sdt")):
        pr = sdt.find(_q("sdtPr"))
        if pr is None:
            continue
        tag_el = pr.find(_q("tag"))
        if tag_el is None:
            continue
        tag = tag_el.get(_q("val"), "")
        if not tag:
            continue

        content = sdt.find(_q("sdtContent"))
        text = ""
        if content is not None:
            text = "".join(t.text or "" for t in content.iter(_q("t")))
        value = _normalize(text)

        # Guard against duplicate tags (a malformed template). Keep both.
        if tag in results:
            dup_counts[tag] = dup_counts.get(tag, 1) + 1
            results[f"{tag}__{dup_counts[tag]}"] = value
        else:
            results[tag] = value

    return results


def _read_docx_from_request(req: func.HttpRequest) -> bytes:
    body = req.get_body()
    if not body:
        raise ValueError("Empty request body.")
    # Try JSON {"fileContentBase64": "..."} first.
    try:
        payload = json.loads(body)
        if isinstance(payload, dict) and "fileContentBase64" in payload:
            return base64.b64decode(payload["fileContentBase64"])
    except (ValueError, TypeError):
        pass
    # Otherwise treat the raw body as the .docx bytes.
    return bytes(body)


@app.route(route="extract", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def extract(req: func.HttpRequest) -> func.HttpResponse:
    try:
        docx_bytes = _read_docx_from_request(req)
    except Exception as e:
        return func.HttpResponse(
            json.dumps({"error": f"Could not read request body: {e}"}),
            status_code=400, mimetype="application/json",
        )

    try:
        fields = extract_content_controls(docx_bytes)
    except zipfile.BadZipFile:
        return func.HttpResponse(
            json.dumps({"error": "Body is not a valid .docx (zip) file."}),
            status_code=400, mimetype="application/json",
        )
    except Exception as e:
        logging.exception("Extraction failed")
        return func.HttpResponse(
            json.dumps({"error": f"Extraction failed: {e}"}),
            status_code=500, mimetype="application/json",
        )

    # Log only the count — never the PHI values.
    logging.info("Extracted %d content-control fields.", len(fields))

    return func.HttpResponse(
        json.dumps({"fieldCount": len(fields), "fields": fields}),
        status_code=200, mimetype="application/json",
    )
