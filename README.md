# WhiteBG-Service (vintage-schatz-bg-removal)

Automated background removal for Google Drive images using **remove.bg API**.
Polls a root folder recursively, finds images without `_weiss` counterparts,
removes background, adds soft shadow, and uploads `_weiss.jpg` back.

## Architecture

```
main.py          ← Main loop + health check + polling
processor.py     ← remove.bg API + quality check + shadow + resize
gdrive.py        ← Drive API + exponential backoff (tenacity)
```

## Pipeline (per image)

1. **Quality Check** — blur + brightness (Pillow, free, no API credit used)
2. **remove.bg API** — crop + scale 85% + center + white BG
3. **Result Check** — detect if API removed too much (>92% white)
4. **Soft Shadow** — subtle drop shadow via Pillow (free)
5. **Resize** — max 2400px, JPEG 90%
6. **Upload** — back to same Drive folder as `originalname_weiss.jpg`

## Key Features

- **No RAM issues**: Cloud API, no local model needed
- **Quality gate**: Bad images skipped before API call (saves credits)
- **Professional shadow**: Subtle drop shadow for eBay/Etsy/Pamono
- **Exponential Backoff**: tenacity retry on Drive API 429/503 errors
- **Idempotent**: Never re-processes files that already have a `_weiss` counterpart
- **Non-destructive**: Originals are NEVER modified or deleted

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `REMOVEBG_API_KEY` | ✅ | - | API key from remove.bg |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | ✅ | - | Full JSON of Service Account key |
| `GDRIVE_ROOT_FOLDER_ID` | ✅ | - | Root folder to poll |
| `POLL_INTERVAL_MINUTES` | ❌ | `60` | Poll interval |
| `LOG_LEVEL` | ❌ | `INFO` | Python log level |
| `DRY_RUN` | ❌ | `false` | Skip upload |
| `PORT` | ❌ | `8080` | Health check port |

## Deploy to Railway

1. Get API key from https://www.remove.bg/api
2. Push repo to GitHub
3. New Railway project → Deploy from GitHub
4. Set env vars (see above)
5. Share Drive root folder with SA email as **Editor**
6. Deploy — no model download needed, starts instantly

## Costs

- ~€0.03–0.04 per image (remove.bg)
- 3,000 images ≈ €90–120 one-time
- Railway Hobby plan: free (500h/month)

## Skip Rules

- `_weiss` in filename → skipped
- `_processing_` prefix → skipped (WF01 lock)
- `-photoroom` in filename → skipped
- Folders: `Glas-Archiv`, `Qualitaet-Pruefen`, `Glas-Bearbeitung-Ausstehend`, `Glas-Fertig` → skipped
