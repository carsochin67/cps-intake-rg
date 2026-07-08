# Local Demo — Proving the CPS Extractor Works (no Azure required)

This lets you demonstrate the whole concept on your own machine, for free, with
no Azure subscription. The extraction logic is **identical** to the production
Azure Function (`function_app.py`) — the only difference is where it runs.

The pitch line: *"This runs locally today and deploys unchanged to our Azure
tenant for production."*

---

## Demo A — One-command extraction (zero installs)

Needs only Python 3 (already on Mac/most machines). No pip, no internet.

```bash
cd azure-function
python demo_extract.py "OFCS-LDSS-2221A Editable (222) CORRECTED.docx"
```

You'll see a summary (191 fields found, how many filled) and every filled field
printed as `Tag = Value`. To show the exact JSON your flow would receive:

```bash
python demo_extract.py "form.docx" --json output.json
python demo_extract.py "form.docx" --all          # include blank fields too
```

This alone proves the core claim: **same file in → same correct fields out,
every time.** Run it on all six samples to show consistency.

---

## Demo B — Live HTTP endpoint (shows the real Power Automate call)

This runs the actual Azure Function on your laptop so you can show the HTTP
request Power Automate would make. Needs two free tools installed once:

1. **Azure Functions Core Tools v4** and **Python 3.11**
   - Mac: `brew tap azure/functions && brew install azure-functions-core-tools@4`
2. Install the Python dependency locally:
   ```bash
   cd azure-function
   pip install -r requirements.txt
   ```

Start the function locally:

```bash
func start
```

It prints a local URL, e.g.:

```
Functions:
    extract: [POST] http://localhost:7071/api/extract
```

Now call it exactly the way Power Automate will — by sending the form's bytes as
base64 (this is what SharePoint's "Get file content" provides):

**Mac / Linux:**
```bash
# base64-encode the docx and POST it as JSON
B64=$(base64 -i "OFCS-LDSS-2221A Editable (222) CORRECTED.docx")
curl -s -X POST http://localhost:7071/api/extract \
  -H "Content-Type: application/json" \
  -d "{\"fileContentBase64\":\"$B64\"}" | python -m json.tool
```

**Windows PowerShell:**
```powershell
$b64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes("OFCS-LDSS-2221A Editable (222) CORRECTED.docx"))
$body = @{ fileContentBase64 = $b64 } | ConvertTo-Json
Invoke-RestMethod -Uri http://localhost:7071/api/extract -Method Post -ContentType "application/json" -Body $body
```

You'll get back the `{ "fieldCount": 191, "fields": { ... } }` JSON — the precise
response your flow's **Parse JSON** step consumes. That demonstrates the complete
pipeline (file → HTTP → clean JSON) end to end, locally.

---

## What this proves for the pitch

- **Reliability:** deterministic extraction, 191/191 fields, no OCR guessing.
- **Speed:** milliseconds per form vs ~11s for AI Builder.
- **Cost:** $0 to demo; effectively $0 in production (Functions free grant).
- **Compliance:** in production it runs in your Azure tenant under the BAA — no
  third-party services, no data stored, PHI never logged.

When you're approved to deploy for real, the same `function_app.py` publishes to
an Azure Function App in your org's subscription, and Power Automate's HTTP step
just points at the cloud URL instead of `localhost`. Nothing else changes.
