# WhiteBG-Service (vintage-schatz-bg-removal)

Automated background removal for Google Drive images.
Polls a root folder recursively, finds images without `_weiss` counterparts,
removes background via rembg, composites onto white #FFFFFF, and uploads `_weiss.jpg` back.

## Architecture

```
main.py          ← Main loop + RAM monitor + health check
processor.py     ← rembg + white background + ONNX thread limits
gdrive.py        ← Drive API + exponential backoff (tenacity)
```

## Key Features

- **RAM Monitor**: Auto-fallback to lighter model when RAM exceeds threshold
- **Model Fallback Chain**: birefnet-general → birefnet-general-lite → isnet-general-use → u2net
- **ONNX Thread Limits**: Prevents CPU throttling on Railway
- **Exponential Backoff**: tenacity retry on Drive API 429/503 errors
- **Idempotent**: Never re-processes files that already have a `_weiss` counterpart
- **Non-destructive**: Originals are NEVER modified or deleted

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | ✅ | - | Full JSON of Service Account key |
| `GDRIVE_ROOT_FOLDER_ID` | ✅ | - | Root folder to poll |
| `REMBG_MODEL` | ❌ | `birefnet-general-lite` | rembg model |
| `POLL_INTERVAL_MINUTES` | ❌ | `60` | Poll interval |
| `MAX_IMAGE_SIZE_PX` | ❌ | `2400` | Resize before rembg |
| `JPEG_QUALITY` | ❌ | `90` | Output JPEG quality |
| `ORT_THREADS` | ❌ | `2` | ONNX intra-op threads |
| `OMP_NUM_THREADS` | ❌ | `2` | OpenMP thread limit |
| `MAX_RAM_PERCENT` | ❌ | `80` | Auto-fallback trigger |
| `LOG_LEVEL` | ❌ | `INFO` | Python log level |
| `DRY_RUN` | ❌ | `false` | Skip upload |
| `PORT` | ❌ | `8080` | Health check port |

## Deploy to Railway

1. Push repo to GitHub
2. New Railway project → Deploy from GitHub
3. Set env vars (see above), especially `GOOGLE_SERVICE_ACCOUNT_JSON`
4. Share Drive root folder with SA email as **Editor**
5. Deploy — model downloads on first start (~30s)

**Tip**: Start with `REMBG_MODEL=birefnet-general-lite` on Hobby plan.
If OOM crashes: switch to `isnet-general-use`.

## Skip Rules

- `_weiss` in filename → skipped
- `_processing_` prefix → skipped (WF01 lock)
- `-photoroom` in filename → skipped
- Folders: `Glas-Archiv`, `Qualitaet-Pruefen`, `Glas-Bearbeitung-Ausstehend`, `Glas-Fertig` → skipped
