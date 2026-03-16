"""
Löscht alle fehlerhaften _weiss-Dateien aus Google Drive,
die VOR dem bgcolor-Fix erstellt wurden (Bild-in-Bild-Fehler).

Nutzung:
  1) DRY-RUN (nur auflisten):  python delete_bad_weiss.py
  2) ECHT LÖSCHEN:              python delete_bad_weiss.py --delete

Braucht die gleichen Env-Vars wie der WhiteBG-Service:
  GOOGLE_REFRESH_TOKEN, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET
"""
import os
import sys
from pathlib import Path
from datetime import datetime, timezone

# .env Datei laden (gleicher Ordner wie dieses Skript)
env_path = Path(__file__).parent / ".env"
if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip())

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# ─── Config ───────────────────────────────────────────────────────
ROOT_FOLDER = "1nJk2cI1FlOX5a5fy5w9JRAODNPuEEwP2"

# Nur Dateien vom 15. März abends + 16. März (vor Fix) löschen.
# Der bgcolor-Fix (ade7e95) wurde ca. 2026-03-16 01:44 UTC aktiv.
CUTOFF_START = datetime(2026, 3, 15, 21, 0, 0, tzinfo=timezone.utc)
CUTOFF_END = datetime(2026, 3, 16, 1, 44, 0, tzinfo=timezone.utc)

SKIP_FOLDERS = {"glas-archiv", "qualitaet-pruefen", "glas-bearbeitung-ausstehend", "glas-fertig"}


def authenticate():
    """OAuth2 auth — gleich wie gdrive.py."""
    refresh_token = os.environ.get("GOOGLE_REFRESH_TOKEN", "")
    client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")

    if not (refresh_token and client_id and client_secret):
        print("FEHLER: Env-Vars nicht gesetzt!")
        print("  GOOGLE_REFRESH_TOKEN, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET")
        sys.exit(1)

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/drive"],
    )
    creds.refresh(Request())
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def find_bad_weiss(service, folder_id, folder_name, do_delete, depth=0):
    """Rekursiv _weiss-Dateien finden und optional löschen."""
    if depth > 10:
        return 0

    count = 0
    page_token = None

    # Alle _weiss-Dateien in diesem Ordner
    while True:
        resp = service.files().list(
            q=f"'{folder_id}' in parents and name contains '_weiss' and trashed=false",
            fields="nextPageToken, files(id, name, createdTime)",
            pageSize=200,
            pageToken=page_token,
        ).execute()

        for f in resp.get("files", []):
            created = datetime.fromisoformat(f["createdTime"].replace("Z", "+00:00"))
            if CUTOFF_START <= created < CUTOFF_END:
                count += 1
                action = "DELETE" if do_delete else "WÜRDE LÖSCHEN"
                print(f"  [{action}] {folder_name}/{f['name']}  (erstellt: {f['createdTime']})")
                if do_delete:
                    service.files().delete(fileId=f["id"]).execute()

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    # Rekursiv in Unterordner
    page_token = None
    while True:
        resp = service.files().list(
            q=f"'{folder_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
            fields="nextPageToken, files(id, name)",
            pageSize=100,
            pageToken=page_token,
        ).execute()

        for sub in resp.get("files", []):
            if sub["name"].lower() in SKIP_FOLDERS:
                continue
            count += find_bad_weiss(service, sub["id"], f"{folder_name}/{sub['name']}", do_delete, depth + 1)

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return count


def main():
    do_delete = "--delete" in sys.argv

    if do_delete:
        if "--force" not in sys.argv:
            print("⚠️  ECHTER LÖSCH-MODUS — Dateien werden unwiderruflich gelöscht!")
            confirm = input("Sicher? Tippe 'JA' zum Fortfahren: ")
            if confirm != "JA":
                print("Abgebrochen.")
                return
        print("🗑️  LÖSCHE fehlerhafte _weiss-Dateien...")
    else:
        print("🔍 DRY-RUN — zeigt nur was gelöscht werden würde.")
        print("   Zum echten Löschen: python delete_bad_weiss.py --delete\n")

    print(f"Zeitfenster: {CUTOFF_START.isoformat()} bis {CUTOFF_END.isoformat()}")
    print(f"Root:   {ROOT_FOLDER}\n")

    service = authenticate()
    print("✅ Authentifiziert\n")

    count = find_bad_weiss(service, ROOT_FOLDER, "ROOT", do_delete)

    print(f"\n{'=' * 50}")
    if do_delete:
        print(f"✅ {count} fehlerhafte _weiss-Dateien GELÖSCHT.")
    else:
        print(f"📋 {count} fehlerhafte _weiss-Dateien gefunden.")
        if count > 0:
            print("   → python delete_bad_weiss.py --delete  zum Löschen")


if __name__ == "__main__":
    main()
