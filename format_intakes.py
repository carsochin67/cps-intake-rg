#!/usr/bin/env python3
"""
Pretty-print the CPS intakes JSON as a table and write a CSV for Excel.

Usage:
    <your command that returns the JSON> | python3 format_intakes.py
    # e.g.
    curl -s "https://.../api/intakes?code=..." | python3 format_intakes.py

    # or from a saved file:
    python3 format_intakes.py < intakes.json

Outputs:
    - an aligned summary table in the terminal (key columns)
    - intakes.csv  (ALL fields, one row per intake -> open in Excel)
"""
import sys, json, csv

# columns to show in the quick terminal table (edit to taste)
SUMMARY_COLS = ["RowKey", "SubmittedAtUtc", "PatientMRN", "PatientEncounter",
                "PatientUnit", "CaseID", "CallID", "Line1_LastName", "Line1_FirstName"]

def main():
    raw = sys.stdin.read().strip()
    if not raw:
        sys.exit("No input. Pipe the JSON into this script.")
    data = json.loads(raw)
    rows = data["intakes"] if isinstance(data, dict) and "intakes" in data else data
    if isinstance(rows, dict):
        rows = [rows]
    if not rows:
        sys.exit("No intakes found in the JSON.")

    # ---- CSV with the union of every field across all rows ----
    keys = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    with open("intakes.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)

    # ---- aligned terminal table (summary columns that actually exist) ----
    cols = [c for c in SUMMARY_COLS if any(c in r for r in rows)]
    table = [cols] + [[str(r.get(c, "")) for c in cols] for r in rows]
    widths = [max(len(row[i]) for row in table) for i in range(len(cols))]
    sep = "-+-".join("-" * w for w in widths)
    for i, row in enumerate(table):
        print(" | ".join(cell.ljust(widths[j]) for j, cell in enumerate(row)))
        if i == 0:
            print(sep)

    print(f"\n{len(rows)} intake(s). Full table with every field written to: intakes.csv")
    print("Open it in Excel, or select the terminal table above to copy/paste.")

if __name__ == "__main__":
    main()
