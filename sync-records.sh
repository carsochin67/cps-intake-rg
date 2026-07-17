#!/usr/bin/env bash
# sync-records.sh — Proof of concept: pull every archived intake PDF out of
# Azure Blob Storage into a local folder that VS Code (and, in the real
# workflow, a shared drive for CM/SW) can browse.
#
# Usage:   bash sync-records.sh
# Re-run any time to refresh the folder with newly submitted intakes.
set -euo pipefail

DEST="$HOME/Desktop/cps-intake-rg/records-pdfs"

echo "1/4  Using the records Function app..."
# Two Function apps exist in this subscription; the web form (config.js
# API_BASE) posts to cps-intake-rg, so that's the one whose storage holds
# the archived PDFs. Pin it explicitly instead of guessing with [0].
APP="cps-intake-rg"
RG="cps-intake-rg"
echo "     App: $APP   Resource group: $RG"

echo "2/4  Reading the records storage account from app settings..."
EP=$(az functionapp config appsettings list -g "$RG" -n "$APP" \
       --query "[?name=='STORAGE_BLOB_ENDPOINT'].value" -o tsv)
ACCT=$(echo "$EP" | sed -E 's#https://([^.]+)\..*#\1#')
# Fall back to the only storage account in the resource group if the app
# setting isn't present.
if [ -z "$ACCT" ]; then
  ACCT=$(az storage account list -g "$RG" --query "[0].name" -o tsv)
fi
echo "     Records storage account: $ACCT   (container: cps-pdfs)"

echo "3/4  Downloading archived PDFs into $DEST ..."
mkdir -p "$DEST"
# Use AAD (--auth-mode login) — this is how the Function wrote the blobs
# (managed identity), and many Function storage accounts have shared-key
# access disabled, which makes --auth-mode key report ContainerNotFound.
# --overwrite re-downloads blobs that already exist locally. Without it the
# batch aborts the moment it hits a file already in $DEST, so re-runs (and the
# daily auto-sync) would fail once the first PDF had been pulled down.
az storage blob download-batch \
  --account-name "$ACCT" \
  --source cps-pdfs \
  --destination "$DEST" \
  --overwrite \
  --auth-mode login

echo "4/4  Done."
echo "Open this folder in VS Code:  $DEST"
find "$DEST" -name '*.pdf' | sort
