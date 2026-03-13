"""
Google Drive API helpers with exponential backoff via tenacity.
Handles authentication, listing, downloading, and uploading.
"""
import io
import os
import json
import logging

from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
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
    """Build Google Drive service.
    
    Prefers OAuth2 refresh token (works with Gmail, uses user's storage quota).
    Falls back to Service Account (SA has zero storage quota on Gmail accounts).
    """
    refresh_token = os.environ.get("GOOGLE_REFRESH_TOKEN", "")
    client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")

    if refresh_token and client_id and client_secret:
        # OAuth2: authenticate as real user → uses their 2TB quota
        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            client_id=client_id,
            client_secret=client_secret,
            token_uri="https://oauth2.googleapis.com/token",
            scopes=["https://www.googleapis.com/auth/drive"],
        )
        creds.refresh(Request())
        service = build("drive", "v3", credentials=creds, cache_discovery=False)
        log.info(f"Authenticated via OAuth2 (user account)")
        return service

    # Fallback: Service Account (read-only useful; uploads fail on Gmail)
    sa_json_str = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if sa_json_str:
        sa_info = json.loads(sa_json_str)
        creds = service_account.Credentials.from_service_account_info(
            sa_info, scopes=["https://www.googleapis.com/auth/drive"]
        )
        service = build("drive", "v3", credentials=creds, cache_discovery=False)
        log.info(f"Authenticated as SA: {sa_info.get('client_email', '?')}")
        log.warning("SA auth: uploads may fail (no storage quota on Gmail accounts)")
        return service

    raise RuntimeError("No auth configured! Set GOOGLE_REFRESH_TOKEN or GOOGLE_SERVICE_ACCOUNT_JSON")


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
