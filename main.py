#!/usr/bin/env python3
"""
WhiteBG-Service: main loop with RAM monitoring and auto-fallback.
Polls Google Drive recursively, removes backgrounds, writes _weiss.jpg back.
"""
import gc
import os
import sys
import json
import time
import logging
import threading
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

import psutil

from gdrive import authenticate, list_subfolders, list_images, download_file, upload_file
from processor import build_session, process_image

# ============================================================
# Configuration
# ============================================================
GDRIVE_ROOT_FOLDER_ID = os.environ.get("GDRIVE_ROOT_FOLDER_ID", "1nJk2cI1FlOX5a5fy5w9JRAODNPuEEwP2")
POLL_INTERVAL_MINUTES = int(os.environ.get("POLL_INTERVAL_MINUTES", "60"))
MAX_IMAGE_SIZE_PX = int(os.environ.get("MAX_IMAGE_SIZE_PX", "2400"))
JPEG_QUALITY = int(os.environ.get("JPEG_QUALITY", "90"))
REMBG_MODEL = os.environ.get("REMBG_MODEL", "birefnet-general-lite")
MAX_RAM_PERCENT = int(os.environ.get("MAX_RAM_PERCENT", "80"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

# Model fallback chain (heavy -> light)
FALLBACK_CHAIN = [
    "birefnet-general",
    "birefnet-general-lite",
    "isnet-general-use",
    "u2net",
]

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
current_model = REMBG_MODEL
rembg_session = None
stats_total = {"processed": 0, "uploaded": 0, "skipped": 0, "errors": 0, "cycles": 0}


# ============================================================
# Health Check Server (Railway needs a listening port)
# ============================================================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({
            "status": "ok",
            "service": "whitebg",
            "model": current_model,
            "ram_percent": psutil.virtual_memory().percent,
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
# RAM Monitor + Auto-Fallback
# ============================================================
def check_ram_and_maybe_fallback():
    """Check RAM usage and fall back to lighter model if needed.
    Returns True if session needs to be reloaded."""
    global current_model
    ram_pct = psutil.virtual_memory().percent
    log.debug(f"RAM usage: {ram_pct:.1f}%")

    if ram_pct > MAX_RAM_PERCENT:
        if current_model in FALLBACK_CHAIN:
            idx = FALLBACK_CHAIN.index(current_model)
            if idx + 1 < len(FALLBACK_CHAIN):
                new_model = FALLBACK_CHAIN[idx + 1]
                log.warning(f"⚠️ RAM {ram_pct:.0f}% > {MAX_RAM_PERCENT}% — Fallback: {current_model} → {new_model}")
                current_model = new_model
                return True
            else:
                log.warning(f"⚠️ RAM {ram_pct:.0f}% > {MAX_RAM_PERCENT}% but already on lightest model ({current_model})")
        else:
            log.warning(f"⚠️ RAM {ram_pct:.0f}% > {MAX_RAM_PERCENT}%, model {current_model} not in fallback chain")
    return False


def load_session():
    """Load or reload the rembg session with the current model."""
    global rembg_session
    log.info(f"Loading model: {current_model} ...")
    try:
        rembg_session = build_session(current_model)
    except Exception as e:
        log.error(f"Failed to load {current_model}: {e}")
        # Try next in fallback chain
        global current_model
        if current_model in FALLBACK_CHAIN:
            idx = FALLBACK_CHAIN.index(current_model)
            for fallback in FALLBACK_CHAIN[idx + 1:]:
                log.info(f"Trying fallback: {fallback}")
                try:
                    current_model = fallback
                    rembg_session = build_session(fallback)
                    return
                except Exception as e2:
                    log.error(f"Fallback {fallback} also failed: {e2}")
        raise RuntimeError("All models failed to load!")


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


# ============================================================
# Folder Processing
# ============================================================
def process_folder(service, folder_id, folder_name, stats):
    """Process all images in a single folder."""
    global rembg_session
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

        to_process.append(img_file)

    if not to_process:
        log.info(f"  No new images to process")
        return

    log.info(f"  {len(to_process)} images to process")

    for img_file in to_process:
        name = img_file["name"]
        file_id = img_file["id"]
        weiss_name = get_weiss_name(name)

        # RAM check before each image
        if check_ram_and_maybe_fallback():
            log.info("Reloading session due to RAM pressure...")
            load_session()

        log.info(f"  🖼️  {name}")
        stats["processed"] += 1

        if DRY_RUN:
            log.info(f"  [DRY RUN] Would create: {weiss_name}")
            continue

        try:
            # Download
            raw_bytes = download_file(service, file_id)
            log.info(f"  Downloaded: {len(raw_bytes):,} bytes")

            # Process
            result_bytes = process_image(
                raw_bytes, rembg_session,
                max_size=MAX_IMAGE_SIZE_PX,
                jpeg_quality=JPEG_QUALITY
            )

            # Upload
            upload_file(service, folder_id, weiss_name, result_bytes)
            stats["uploaded"] += 1

            # Cleanup
            del raw_bytes, result_bytes
            gc.collect()

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
    log.info("WhiteBG-Service starting")
    log.info(f"  Root folder:  {GDRIVE_ROOT_FOLDER_ID}")
    log.info(f"  Model:        {REMBG_MODEL}")
    log.info(f"  Poll:         {POLL_INTERVAL_MINUTES} min")
    log.info(f"  Max size:     {MAX_IMAGE_SIZE_PX}px")
    log.info(f"  JPEG quality: {JPEG_QUALITY}")
    log.info(f"  Max RAM:      {MAX_RAM_PERCENT}%")
    log.info(f"  Dry run:      {DRY_RUN}")
    log.info(f"  RAM now:      {psutil.virtual_memory().percent:.1f}%")
    log.info("=" * 60)

    start_health_server()
    load_session()

    service = authenticate()

    while True:
        try:
            cycle_stats = {"processed": 0, "uploaded": 0, "skipped": 0, "errors": 0}
            log.info(f"\n{'='*30} POLL START {'='*30}")
            log.info(f"RAM: {psutil.virtual_memory().percent:.1f}% | Model: {current_model}")

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
            log.info(f"  RAM:       {psutil.virtual_memory().percent:.1f}%")

        except Exception as e:
            log.error(f"Poll cycle error: {e}", exc_info=True)

        log.info(f"Sleeping {POLL_INTERVAL_MINUTES} minutes...")
        time.sleep(POLL_INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    main()
