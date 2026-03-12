#!/usr/bin/env python3
"""
WhiteBG-Service: Recursive background removal for Google Drive images.
Polls a Google Drive root folder, finds images without _weiss counterparts,
removes background via rembg, and writes _weiss.jpg back to the same folder.
"""
import io
import os
import sys
import json
import time
import logging
import tempfile
from pathlib import Path

from PIL import Image
from rembg import remove, new_session
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

# ============================================================
# Configuration via Environment Variables
# ============================================================
GDRIVE_ROOT_FOLDER_ID = os.environ.get("GDRIVE_ROOT_FOLDER_ID", "1nJk2cI1FlOX5a5fy5w9JRAODNPuEEwP2")
POLL_INTERVAL_MINUTES = int(os.environ.get("POLL_INTERVAL_MINUTES", "60"))
MAX_IMAGE_SIZE_PX = int(os.environ.get("MAX_IMAGE_SIZE_PX", "2400"))
JPEG_QUALITY = int(os.environ.get("JPEG_QUALITY", "90"))
REMBG_MODEL = os.environ.get("REMBG_MODEL", "birefnet-general")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

# Image MIME types we process
IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "image/tiff", "image/bmp"}

# ============================================================
# Logging
# ============================================================
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout
)
log = logging.getLogger("whitebg")

# ============================================================
# Google Drive Authentication
# ============================================================
def get_drive_service():
    """Authenticate with Google Drive using Service Account JSON from env."""
    sa_json_str = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not sa_json_str:
        sa_json_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON_PATH", "")
        if sa_json_path and Path(sa_json_path).exists():
            sa_json_str = Path(sa_json_path).read_text(encoding="utf-8")
        else:
            raise RuntimeError(
                "GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_SERVICE_ACCOUNT_JSON_PATH must be set"
            )

    sa_info = json.loads(sa_json_str)
    creds = service_account.Credentials.from_service_account_info(
        sa_info, scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds)


# ============================================================
# Drive Helpers
# ============================================================
def list_subfolders(service, folder_id):
    """List all subfolders (non-trashed) in a given folder."""
    results = []
    page_token = None
    while True:
        resp = service.files().list(
            q=f"'{folder_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
            fields="nextPageToken, files(id, name)",
            pageSize=100,
            pageToken=page_token
        ).execute()
        results.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return results


def list_images(service, folder_id):
    """List all image files (non-trashed) in a given folder."""
    results = []
    page_token = None
    q_parts = [f"'{folder_id}' in parents", "trashed=false"]
    mime_clauses = " or ".join(f"mimeType='{m}'" for m in IMAGE_MIMES)
    q_parts.append(f"({mime_clauses})")
    query = " and ".join(q_parts)

    while True:
        resp = service.files().list(
            q=query,
            fields="nextPageToken, files(id, name, mimeType, size)",
            pageSize=200,
            pageToken=page_token
        ).execute()
        results.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return results


def download_file(service, file_id):
    """Download a file from Google Drive, return bytes."""
    request = service.files().get_media(fileId=file_id)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    buffer.seek(0)
    return buffer.read()


def upload_file(service, folder_id, filename, data_bytes, mime_type="image/jpeg"):
    """Upload a file to a specific Google Drive folder."""
    file_metadata = {
        "name": filename,
        "parents": [folder_id]
    }
    media = MediaIoBaseUpload(io.BytesIO(data_bytes), mimetype=mime_type, resumable=True)
    created = service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id, name"
    ).execute()
    return created


# ============================================================
# Image Processing
# ============================================================
def remove_background(image_bytes, session):
    """Remove background from image bytes, return JPEG bytes."""
    img = Image.open(io.BytesIO(image_bytes))

    # Resize if too large
    w, h = img.size
    if max(w, h) > MAX_IMAGE_SIZE_PX:
        ratio = MAX_IMAGE_SIZE_PX / max(w, h)
        new_w, new_h = int(w * ratio), int(h * ratio)
        img = img.resize((new_w, new_h), Image.LANCZOS)
        log.info(f"  Resized {w}x{h} -> {new_w}x{new_h}")

    # Convert to RGB for rembg input
    if img.mode != "RGB":
        img = img.convert("RGB")

    # Remove background
    input_buf = io.BytesIO()
    img.save(input_buf, format="PNG")
    input_buf.seek(0)

    result_bytes = remove(input_buf.getvalue(), session=session)
    result_img = Image.open(io.BytesIO(result_bytes)).convert("RGBA")

    # Create white background version
    white_bg = Image.new("RGBA", result_img.size, (255, 255, 255, 255))
    white_bg.paste(result_img, mask=result_img.split()[3])
    final = white_bg.convert("RGB")

    # Save as JPEG
    output_buf = io.BytesIO()
    final.save(output_buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    output_buf.seek(0)
    return output_buf.getvalue()


# ============================================================
# Main Processing Logic
# ============================================================
def get_weiss_name(original_name):
    """Generate _weiss filename from original name.
    Example: 'photo.jpg' -> 'photo_weiss.jpg'
    """
    stem = Path(original_name).stem
    return f"{stem}_weiss.jpg"


def should_skip(filename):
    """Check if a file should be skipped."""
    name_lower = filename.lower()
    # Skip _weiss files
    if "_weiss" in name_lower:
        return True
    # Skip _processing_ files (WF01 is working on them)
    if name_lower.startswith("_processing_"):
        return True
    # Skip Photoroom files
    if "-photoroom" in name_lower:
        return True
    return False


def process_folder(service, folder_id, folder_name, session, stats):
    """Process all images in a folder, creating _weiss versions where missing."""
    log.info(f"📁 Processing folder: {folder_name} ({folder_id})")

    images = list_images(service, folder_id)
    if not images:
        log.info(f"  No images found in {folder_name}")
        return

    # Build set of existing filenames for quick lookup
    existing_names = {f["name"].lower() for f in images}

    for img_file in images:
        name = img_file["name"]
        file_id = img_file["id"]

        # Skip files that shouldn't be processed
        if should_skip(name):
            log.debug(f"  Skipping: {name}")
            stats["skipped"] += 1
            continue

        # Check if _weiss version already exists
        weiss_name = get_weiss_name(name)
        if weiss_name.lower() in existing_names:
            log.debug(f"  Already has _weiss: {name}")
            stats["already_done"] += 1
            continue

        # Process this image
        log.info(f"  🖼️  Processing: {name}")
        stats["processed"] += 1

        if DRY_RUN:
            log.info(f"  [DRY RUN] Would create: {weiss_name}")
            continue

        try:
            # Download
            raw_bytes = download_file(service, file_id)
            log.info(f"  Downloaded {len(raw_bytes)} bytes")

            # Remove background
            result_bytes = remove_background(raw_bytes, session)
            log.info(f"  Background removed, result: {len(result_bytes)} bytes")

            # Upload _weiss version
            uploaded = upload_file(service, folder_id, weiss_name, result_bytes)
            log.info(f"  ✅ Uploaded: {uploaded['name']} (id: {uploaded['id']})")
            stats["uploaded"] += 1

        except Exception as e:
            log.error(f"  ❌ Error processing {name}: {e}")
            stats["errors"] += 1


def process_recursive(service, folder_id, folder_name, session, stats, depth=0):
    """Recursively process a folder and all its subfolders."""
    if depth > 10:
        log.warning(f"  Max depth reached at {folder_name}, skipping")
        return

    # Process images in current folder
    process_folder(service, folder_id, folder_name, session, stats)

    # Recurse into subfolders
    subfolders = list_subfolders(service, folder_id)
    for sf in subfolders:
        sf_name = sf["name"]
        # Skip special folders
        if sf_name.lower() in {"glas-archiv", "qualitaet-pruefen"}:
            log.info(f"  Skipping special folder: {sf_name}")
            continue
        process_recursive(service, sf["id"], sf_name, session, stats, depth + 1)


# ============================================================
# Health Check Server (for Railway)
# ============================================================
def start_health_server():
    """Start a minimal HTTP health check server in a background thread."""
    import threading
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok", "service": "whitebg"}).encode())

        def log_message(self, format, *args):
            pass  # Suppress default logging

    port = int(os.environ.get("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info(f"Health check server running on port {port}")


# ============================================================
# Main Loop
# ============================================================
def main():
    log.info("=" * 60)
    log.info("WhiteBG-Service starting")
    log.info(f"  Root folder: {GDRIVE_ROOT_FOLDER_ID}")
    log.info(f"  Model: {REMBG_MODEL}")
    log.info(f"  Poll interval: {POLL_INTERVAL_MINUTES} min")
    log.info(f"  Max image size: {MAX_IMAGE_SIZE_PX}px")
    log.info(f"  JPEG quality: {JPEG_QUALITY}")
    log.info(f"  Dry run: {DRY_RUN}")
    log.info("=" * 60)

    # Start health check endpoint
    start_health_server()

    # Initialize rembg session (loads model once)
    log.info(f"Loading rembg model: {REMBG_MODEL} ...")
    try:
        session = new_session(REMBG_MODEL)
        log.info("Model loaded successfully")
    except Exception as e:
        log.error(f"Failed to load model {REMBG_MODEL}: {e}")
        log.info("Falling back to isnet-general-use")
        session = new_session("isnet-general-use")

    # Initialize Drive service
    service = get_drive_service()
    log.info("Google Drive authenticated")

    # Main polling loop
    while True:
        try:
            stats = {"processed": 0, "uploaded": 0, "skipped": 0, "already_done": 0, "errors": 0}
            log.info(f"\n{'='*40} POLL START {'='*40}")

            process_recursive(service, GDRIVE_ROOT_FOLDER_ID, "ROOT", session, stats)

            log.info(f"\n--- Poll Summary ---")
            log.info(f"  Processed: {stats['processed']}")
            log.info(f"  Uploaded:  {stats['uploaded']}")
            log.info(f"  Skipped:   {stats['skipped']}")
            log.info(f"  Existing:  {stats['already_done']}")
            log.info(f"  Errors:    {stats['errors']}")

        except Exception as e:
            log.error(f"Poll cycle error: {e}", exc_info=True)

        log.info(f"Sleeping {POLL_INTERVAL_MINUTES} minutes until next poll...")
        time.sleep(POLL_INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    main()
