#!/usr/bin/env python3
"""
WhiteBG-Service: Polls Google Drive, removes backgrounds via remove.bg API,
uploads _weiss.jpg back to the same folder. Non-destructive.
"""
import os
import sys
import json
import time
import logging
import threading
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

import requests as http_requests

from gdrive import authenticate, list_subfolders, list_images, download_file, upload_file
from processor import process_image


def send_telegram(text: str):
    """Send a message via Telegram bot. Silently fails if not configured."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        http_requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
        }, timeout=10)
    except Exception as e:
        logging.getLogger("whitebg").warning(f"Telegram send failed: {e}")

# ============================================================
# Configuration
# ============================================================
GDRIVE_ROOT_FOLDER_ID = os.environ.get("GDRIVE_ROOT_FOLDER_ID", "1nJk2cI1FlOX5a5fy5w9JRAODNPuEEwP2")
# rembg URL is configured in processor.py via REMBG_URL env var
POLL_INTERVAL_MINUTES = int(os.environ.get("POLL_INTERVAL_MINUTES", "60"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

# Telegram notifications
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Airtable config for measurement photo cache
AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN", "")
AIRTABLE_BASE = os.environ.get("AIRTABLE_BASE", "appWh8CQNQbpI1tLJ")
AIRTABLE_PHOTOS_TABLE = os.environ.get("AIRTABLE_PHOTOS_TABLE", "tblXd0poan6Sz53TR")

# Folders to skip during recursive scan
SKIP_FOLDERS = {"glas-archiv", "qualitaet-pruefen", "glas-bearbeitung-ausstehend", "glas-fertig"}

# ============================================================
# Logging
# ============================================================
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("whitebg")

# ============================================================
# Global state
# ============================================================
stats_total = {"processed": 0, "uploaded": 0, "skipped": 0, "errors": 0, "cycles": 0}


# ============================================================
# Health Check Server (Railway needs a listening port)
# ============================================================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({
            "status": "ok",
            "service": "whitebg-rembg",
            "stats": stats_total,
        })
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, format, *args):
        pass  # suppress default logging


def start_health_server():
    port = int(os.environ.get("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info(f"Health check server on port {port}")


# ============================================================
# File Naming Helpers
# ============================================================
def get_weiss_name(original_name):
    """photo.jpg -> photo_weiss.jpg"""
    stem = Path(original_name).stem
    return f"{stem}_weiss.jpg"


def should_skip(filename):
    """Check if a file should be skipped from processing."""
    name_lower = filename.lower()
    if "_weiss" in name_lower:
        return True
    if name_lower.startswith("_processing_"):
        return True
    if "-photoroom" in name_lower:
        return True
    return False


def is_measurement_photo_cached(filename):
    """Check Airtable cache if this photo is a measurement photo (Zollstock).
    Returns True if cached as measurement, False otherwise.
    Returns False on any error (fail-open: process the photo if unsure).
    """
    if not AIRTABLE_TOKEN:
        return False
    try:
        url = f"https://api.airtable.com/v0/{AIRTABLE_BASE}/{AIRTABLE_PHOTOS_TABLE}"
        safe_name = filename.replace("'", "\\'")
        params = {
            "filterByFormula": f"AND({{Photo Name}}='{safe_name}',{{Ruler_Checked}}=TRUE(),{{Is_Measurement_Photo}}=TRUE())",
            "maxRecords": "1",
            "fields[]": ["Photo Name", "Is_Measurement_Photo"],
        }
        headers = {"Authorization": f"Bearer {AIRTABLE_TOKEN}"}
        resp = http_requests.get(url, params=params, headers=headers, timeout=10)
        if resp.status_code == 200:
            records = resp.json().get("records", [])
            if records:
                log.info(f"  ⏭️ Airtable: {filename} is measurement photo (cached)")
                return True
        return False
    except Exception as e:
        log.warning(f"  Airtable check failed for {filename}: {e}")
        return False


# ============================================================
# Folder Processing
# ============================================================
def process_folder(service, folder_id, folder_name, stats):
    """Process all images in a single folder."""
    log.info(f"📁 {folder_name} ({folder_id})")

    images = list_images(service, folder_id)
    if not images:
        log.info(f"  (empty)")
        return

    # Build lookup of existing filenames
    existing_names = {f["name"].lower() for f in images}
    to_process = []

    for img_file in images:
        name = img_file["name"]

        if should_skip(name):
            stats["skipped"] += 1
            continue

        weiss_name = get_weiss_name(name)
        if weiss_name.lower() in existing_names:
            stats["skipped"] += 1
            continue

        # Skip measurement photos (Zollstock) via Airtable cache
        if is_measurement_photo_cached(name):
            stats["skipped"] += 1
            continue

        to_process.append(img_file)

    if not to_process:
        log.info(f"  No new images to process")
        return

    log.info(f"  {len(to_process)} images to process")

    for img_file in to_process:
        name = img_file["name"]
        file_id = img_file["id"]
        weiss_name = get_weiss_name(name)

        log.info(f"  🖼️  {name}")
        stats["processed"] += 1

        if DRY_RUN:
            log.info(f"  [DRY RUN] Would create: {weiss_name}")
            continue

        try:
            # Download
            raw_bytes = download_file(service, file_id)
            log.info(f"  Downloaded: {len(raw_bytes):,} bytes")

            # Process via rembg pipeline (free, self-hosted)
            result_bytes, status = process_image(raw_bytes, name)

            if result_bytes is None:
                log.warning(f"  ⏭️ {status}")
                stats["skipped"] += 1
                continue

            log.info(f"  ✅ {status} — {len(result_bytes):,} bytes")

            # Upload
            upload_file(service, folder_id, weiss_name, result_bytes)
            stats["uploaded"] += 1

        except Exception as e:
            log.error(f"  ❌ Error: {name} — {e}", exc_info=True)
            stats["errors"] += 1


def process_recursive(service, folder_id, folder_name, stats, depth=0):
    """Recursively process a folder tree."""
    if depth > 10:
        log.warning(f"Max depth reached at {folder_name}")
        return

    process_folder(service, folder_id, folder_name, stats)

    subfolders = list_subfolders(service, folder_id)
    for sf in subfolders:
        sf_name = sf["name"]
        if sf_name.lower() in SKIP_FOLDERS:
            log.info(f"  ⏭️ Skipping folder: {sf_name}")
            continue
        process_recursive(service, sf["id"], sf_name, stats, depth + 1)


# ============================================================
# Main
# ============================================================
def main():
    log.info("=" * 60)
    log.info("WhiteBG-Service (rembg, free) starting")
    log.info(f"  Root folder:  {GDRIVE_ROOT_FOLDER_ID}")
    log.info(f"  rembg URL:    {os.environ.get('REMBG_URL', 'https://rembg-new-production.up.railway.app')}")
    log.info(f"  Poll:         {POLL_INTERVAL_MINUTES} min")
    log.info(f"  Dry run:      {DRY_RUN}")
    log.info("=" * 60)

    start_health_server()
    service = authenticate()

    while True:
        try:
            cycle_stats = {"processed": 0, "uploaded": 0, "skipped": 0, "errors": 0}
            log.info(f"\n{'='*30} POLL START {'='*30}")

            process_recursive(service, GDRIVE_ROOT_FOLDER_ID, "ROOT", cycle_stats)

            stats_total["processed"] += cycle_stats["processed"]
            stats_total["uploaded"] += cycle_stats["uploaded"]
            stats_total["skipped"] += cycle_stats["skipped"]
            stats_total["errors"] += cycle_stats["errors"]
            stats_total["cycles"] += 1

            log.info(f"\n--- Cycle Summary ---")
            log.info(f"  Processed: {cycle_stats['processed']}")
            log.info(f"  Uploaded:  {cycle_stats['uploaded']}")
            log.info(f"  Skipped:   {cycle_stats['skipped']}")
            log.info(f"  Errors:    {cycle_stats['errors']}")

            # Telegram-Summary (nur wenn etwas passiert ist)
            if cycle_stats["processed"] > 0 or cycle_stats["errors"] > 0:
                emoji = "✅" if cycle_stats["errors"] == 0 else "⚠️"
                send_telegram(
                    f"{emoji} <b>WhiteBG Zyklus #{stats_total['cycles']}</b>\n"
                    f"🖼 Verarbeitet: {cycle_stats['processed']}\n"
                    f"⬆️ Hochgeladen: {cycle_stats['uploaded']}\n"
                    f"⏭️ Übersprungen: {cycle_stats['skipped']}\n"
                    f"❌ Fehler: {cycle_stats['errors']}"
                )

        except Exception as e:
            log.error(f"Poll cycle error: {e}", exc_info=True)

        log.info(f"Sleeping {POLL_INTERVAL_MINUTES} minutes...")
        time.sleep(POLL_INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    main()
