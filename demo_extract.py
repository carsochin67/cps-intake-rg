#!/usr/bin/env python3
"""
LOCAL DEMO — Deterministic CPS form extractor (no Azure, no internet, stdlib only).

Run it against any of the .docx forms to see the clean field output that the
Azure Function would return in production. The extraction logic here is identical
to function_app.py — this script just lets you demo it on your own machine.

Usage:
    python demo_extract.py "OFCS-LDSS-2221 Editable (221).docx"
    python demo_extract.py form.docx --all      # print every field
    python demo_extract.py form.docx --json out.json   # also write JSON file

Proves the concept for a pitch: same file in -> same correct fields out, every time.
"""
import sys
import io
import json
import zipfile
import xml.etree.ElementTree as ET

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _q(tag: str) -> str:
    return f"{{{W}}}{tag}"


def _normalize(value: str) -> str:
    return (value or "").replace("☐", "").strip()


def extract_content_controls(docx_bytes: bytes) -> dict:
    """Return {tag: value} for every <w:sdt> content control in the document."""
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as z:
        xml = z.read("word/document.xml")
    root = ET.fromstring(xml)
    results, dups = {}, {}
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
        text = "".join(t.text or "" for t in content.iter(_q("t"))) if content is not None else ""
        value = _normalize(text)
        if tag in results:
            dups[tag] = dups.get(tag, 1) + 1
            results[f"{tag}__{dups[tag]}"] = value
        else:
            results[tag] = value
    return results


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    show_all = "--all" in sys.argv
    json_out = None
    if "--json" in sys.argv:
        i = sys.argv.index("--json")
        if i + 1 < len(sys.argv):
            json_out = sys.argv[i + 1]
            args = [a for a in args if a != json_out]

    if not args:
        print(__doc__)
        sys.exit(1)

    path = args[0]
    try:
        data = open(path, "rb").read()
    except OSError as e:
        print(f"Could not open file: {e}")
        sys.exit(1)

    fields = extract_content_controls(data)
    filled = {k: v for k, v in fields.items() if v}

    print("=" * 60)
    print(f"  FILE: {path}")
    print(f"  Fields found:  {len(fields)}")
    print(f"  Fields filled: {len(filled)}  (blank: {len(fields) - len(filled)})")
    print("=" * 60)

    items = fields.items() if show_all else filled.items()
    label = "ALL FIELDS" if show_all else "FILLED FIELDS"
    print(f"\n{label}:\n")
    for k, v in items:
        print(f"  {k:32} = {v}")

    if json_out:
        payload = {"fieldCount": len(fields), "fields": fields}
        with open(json_out, "w") as f:
            json.dump(payload, f, indent=2)
        print(f"\nFull JSON (what the Azure Function returns) written to: {json_out}")
    else:
        print("\nTip: add  --json out.json  to save the exact payload Power Automate would receive.")


if __name__ == "__main__":
    main()
