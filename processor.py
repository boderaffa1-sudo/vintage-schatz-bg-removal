"""
Image processing via remove.bg API.
Pipeline: Quality Check → remove.bg (with shadow) → Result Check → Resize
"""
import io
import logging
import time

import numpy as np
import requests
from PIL import Image

log = logging.getLogger("whitebg.processor")


# ============================================================
# Step 1: Quality Check BEFORE API (free, no credit used)
# ============================================================
def check_quality(image_bytes: bytes) -> tuple:
    """Check blur + brightness. Returns (ok, reason)."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    # Blur check (Laplacian-like via numpy diffs)
    gray = img.convert("L")
    arr = np.array(gray, dtype=float)
    laplacian = np.abs(np.diff(np.diff(arr, axis=0), axis=0)).mean()
    if laplacian < 1.0:
        return False, f"Zu unscharf (Score: {laplacian:.1f})"
    # Brightness check
    brightness = np.array(img).mean()
    if brightness < 30:
        return False, f"Zu dunkel (Helligkeit: {brightness:.0f})"
    return True, "OK"


# ============================================================
# Step 2: remove.bg API
# ============================================================
def remove_background(image_bytes: bytes, api_key: str, max_retries: int = 4) -> bytes:
    """Call remove.bg API with crop + scale + white BG. Retries on 429."""
    for attempt in range(max_retries + 1):
        response = requests.post(
            "https://api.remove.bg/v1.0/removebg",
            files={"image_file": ("image.jpg", image_bytes, "image/jpeg")},
            data={
                "size": "auto",
                "type": "product",
                "crop": "true",
                "crop_margin": "5%",
                "scale": "85%",
                "position": "center",
                "bg_color": "ffffff",
                "format": "jpg",
                "shadow_type": "drop",
                "shadow_opacity": "50",
            },
            headers={"X-Api-Key": api_key},
            timeout=60,
        )
        if response.status_code == 200:
            return response.content
        if response.status_code == 429 and attempt < max_retries:
            # Use Retry-After header if available, otherwise fallback
            wait = int(response.headers.get("Retry-After", 5 * (2 ** attempt)))
            remaining = response.headers.get("X-RateLimit-Remaining", "?")
            log.warning(f"  Rate limit 429, Retry-After={wait}s, remaining={remaining} (attempt {attempt+1}/{max_retries})")
            time.sleep(wait + 1)  # +1s safety margin
            continue
        raise Exception(f"remove.bg Fehler: {response.status_code} {response.text}")
    raise Exception("remove.bg: max retries exceeded")


# ============================================================
# Step 3: Result Check (too much removed?)
# ============================================================
def check_result(image_bytes: bytes) -> tuple:
    """Check if API removed too much (>92% white)."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    arr = np.array(img)
    white_mask = (arr[:, :, 0] > 240) & (arr[:, :, 1] > 240) & (arr[:, :, 2] > 240)
    white_ratio = white_mask.mean()
    if white_ratio > 0.92:
        return False, f"API hat zu viel entfernt ({white_ratio*100:.0f}% weiß)"
    return True, "OK"


# ============================================================
# Step 4: Resize to max 2400px
# ============================================================
def resize_final(image_bytes: bytes, max_px: int = 2400) -> bytes:
    """Resize to max_px, never upscale."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    if max(img.size) > max_px:
        img.thumbnail((max_px, max_px), Image.LANCZOS)
    output = io.BytesIO()
    img.save(output, format="JPEG", quality=90)
    return output.getvalue()


# ============================================================
# Main pipeline
# ============================================================
def process_image(image_bytes: bytes, filename: str, api_key: str) -> tuple:
    """
    Full pipeline: Quality → remove.bg (with shadow) → Result Check → Resize.
    Returns (result_bytes | None, status_message).
    """
    # 1. Quality check (free, no API credit used on failure)
    ok, reason = check_quality(image_bytes)
    if not ok:
        return None, f"SKIP Qualität: {reason}"

    # 2. remove.bg API
    try:
        result = remove_background(image_bytes, api_key)
    except Exception as e:
        return None, f"FEHLER API: {e}"

    # Rate limit protection: 2s between API calls
    time.sleep(2)

    # 3. Result check
    ok, reason = check_result(result)
    if not ok:
        return None, f"SKIP Ergebnis: {reason}"

    # 4. Resize (shadow is now done by remove.bg API)
    result = resize_final(result)

    return result, "OK"
