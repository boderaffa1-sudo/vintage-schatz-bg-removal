"""
Google Drive API helpers with exponential backoff via tenacity.
Handles authentication, listing, downloading, and uploading.
"""
import io
import os
import json
import logging

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
from googleapiclient.errors import HttpError
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential_jitter,
    retry_if_exception_type,
    before_sleep_log,
)

log = logging.getLogger("whitebg.gdrive")

# Image MIME types we process
IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "image/tiff", "image/bmp"}

# Retry decorator for all Drive API calls
_retry_drive = retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential_jitter(initial=1, max=32),
    retry=retry_if_exception_type((HttpError, ConnectionError, TimeoutError, OSError)),
    before_sleep=before_sleep_log(log, logging.WARNING),
    reraise=True,
)


def authenticate():
    """Build Google Drive service from Service Account JSON in env var."""
    sa_json_str = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not sa_json_str:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON env var is not set")

    log.info(f"SA JSON length: {len(sa_json_str)} chars")
    log.info(f"SA JSON first 80 chars: {sa_json_str[:80]}")
    sa_info = json.loads(sa_json_str)
    log.info(f"SA JSON keys: {list(sa_info.keys())}")
    log.info(f"Has token_uri: {'token_uri' in sa_info}")
    creds = service_account.Credentials.from_service_account_info(
        sa_info, scopes=["https://www.googleapis.com/auth/drive"]
    )
    service = build("drive", "v3", credentials=creds, cache_discovery=False)
    log.info(f"Authenticated as {sa_info.get('client_email', '?')}")
    return service


@_retry_drive
def list_subfolders(service, folder_id):
    """List all non-trashed subfolders in a folder."""
    results = []
    page_token = None
    while True:
        resp = service.files().list(
            q=f"'{folder_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
            fields="nextPageToken, files(id, name)",
            pageSize=100,
            pageToken=page_token,
        ).execute()
        results.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return results


@_retry_drive
def list_images(service, folder_id):
    """List all image files (non-trashed) in a folder."""
    results = []
    page_token = None
    mime_clauses = " or ".join(f"mimeType='{m}'" for m in IMAGE_MIMES)
    query = f"'{folder_id}' in parents and trashed=false and ({mime_clauses})"

    while True:
        resp = service.files().list(
            q=query,
            fields="nextPageToken, files(id, name, mimeType, size)",
            pageSize=200,
            pageToken=page_token,
        ).execute()
        results.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return results


@_retry_drive
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


@_retry_drive
def upload_file(service, folder_id, filename, data_bytes, mime_type="image/jpeg"):
    """Upload a file to a specific Google Drive folder."""
    file_metadata = {"name": filename, "parents": [folder_id]}
    media = MediaIoBaseUpload(
        io.BytesIO(data_bytes), mimetype=mime_type, resumable=True
    )
    created = (
        service.files()
        .create(body=file_metadata, media_body=media, fields="id, name")
        .execute()
    )
    log.info(f"Uploaded: {created['name']} (id: {created['id']})")
    return created
