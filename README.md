# WhiteBG-Service (vintage-schatz-bg-removal)

Automated background removal service for Google Drive images.
Polls a root folder recursively, finds images without `_weiss` counterparts,
removes background via rembg, and uploads `_weiss.jpg` back to the same folder.

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | ✅ | - | Full JSON string of the Service Account key |
| `GDRIVE_ROOT_FOLDER_ID` | ✅ | - | Google Drive root folder ID to poll |
| `POLL_INTERVAL_MINUTES` | ❌ | `60` | Minutes between poll cycles |
| `MAX_IMAGE_SIZE_PX` | ❌ | `2400` | Max dimension before resize |
| `JPEG_QUALITY` | ❌ | `90` | JPEG output quality (1-100) |
| `REMBG_MODEL` | ❌ | `birefnet-general` | rembg model name |
| `LOG_LEVEL` | ❌ | `INFO` | Python log level |
| `DRY_RUN` | ❌ | `false` | If true, skip upload |
| `PORT` | ❌ | `8080` | Health check port |

## Deploy to Railway

1. Push this repo to GitHub
2. Create new Railway project from GitHub repo
3. Set environment variables (see above)
4. Share the Google Drive root folder with the Service Account email as **Editor**
5. Deploy

## Skip Rules

- Files with `_weiss` in name → skipped
- Files starting with `_processing_` → skipped (WF01 lock)
- Files with `-photoroom` in name → skipped
- Folders named `Glas-Archiv` or `Qualitaet-Pruefen` → skipped
