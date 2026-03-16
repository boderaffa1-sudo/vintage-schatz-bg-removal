"""Analyze creation dates of _weiss files to find the bad batch."""
import os
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# .env laden
env_path = Path(__file__).parent / ".env"
for line in env_path.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip())

creds = Credentials(
    token=None,
    refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
    client_id=os.environ["GOOGLE_CLIENT_ID"],
    client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
    token_uri="https://oauth2.googleapis.com/token",
    scopes=["https://www.googleapis.com/auth/drive"],
)
creds.refresh(Request())
service = build("drive", "v3", credentials=creds, cache_discovery=False)

ROOT = "1nJk2cI1FlOX5a5fy5w9JRAODNPuEEwP2"
CUTOFF = datetime(2026, 3, 16, 1, 44, 0, tzinfo=timezone.utc)
SKIP = {"glas-archiv", "qualitaet-pruefen", "glas-bearbeitung-ausstehend", "glas-fertig"}

all_files = []

def scan(folder_id, path, depth=0):
    if depth > 10:
        return
    page_token = None
    while True:
        resp = service.files().list(
            q=f"'{folder_id}' in parents and name contains '_weiss' and trashed=false",
            fields="nextPageToken, files(id, name, createdTime)",
            pageSize=200, pageToken=page_token).execute()
        for f in resp.get("files", []):
            ct = datetime.fromisoformat(f["createdTime"].replace("Z", "+00:00"))
            if ct < CUTOFF:
                all_files.append((ct, f["name"], path))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    page_token = None
    while True:
        resp = service.files().list(
            q=f"'{folder_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
            fields="nextPageToken, files(id, name)",
            pageSize=100, pageToken=page_token).execute()
        for sub in resp.get("files", []):
            if sub["name"].lower() not in SKIP:
                scan(sub["id"], f"{path}/{sub['name']}", depth + 1)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

scan(ROOT, "ROOT")
all_files.sort()

date_counts = Counter()
for ct, name, path in all_files:
    date_counts[ct.strftime("%Y-%m-%d")] += 1

print("=== Dateien pro Tag ===")
for date, count in sorted(date_counts.items()):
    print(f"  {date}: {count} Dateien")

print(f"\nTotal: {len(all_files)}")

for date in sorted(date_counts.keys()):
    hour_counts = Counter()
    for ct, name, path in all_files:
        if ct.strftime("%Y-%m-%d") == date:
            hour_counts[ct.strftime("%H:00")] += 1
    print(f"\n=== {date} (Stunden) ===")
    for hour, count in sorted(hour_counts.items()):
        print(f"  {hour}: {count}")
