"""
Image processing via remove.bg API.
Pipeline: Quality Check → remove.bg → Result Check → Shadow → Resize
"""
import io
import logging
import time

import numpy as np
import requests
from PIL import Image, ImageFilter

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
def remove_background(image_bytes: bytes, api_key: str) -> bytes:
    """Call remove.bg API with crop + scale + white BG."""
    response = requests.post(
        "https://api.remove.bg/v1.0/removebg",
        files={"image_file": ("image.jpg", image_bytes, "image/jpeg")},
        data={
            "size": "auto",
            "crop": "true",
            "crop_margin": "5%",
            "scale": "85%",
            "position": "center",
            "bg_color": "ffffff",
            "format": "jpg",
        },
        headers={"X-Api-Key": api_key},
        timeout=60,
    )
    if response.status_code != 200:
        raise Exception(f"remove.bg Fehler: {response.status_code} {response.text}")
    return response.content


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
# Step 4: Soft Shadow via Pillow
# ============================================================
def add_shadow(image_bytes: bytes) -> bytes:
    """Add subtle drop shadow under the object."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    shadow_offset = (8, 12)
    shadow_blur = 18
    shadow_opacity = 60
    w, h = img.size
    canvas = Image.new("RGBA", (w + 40, h + 40), (255, 255, 255, 255))
    # Create shadow from alpha channel
    alpha = img.split()[3]
    shadow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    shadow.putalpha(alpha)
    shadow = shadow.filter(ImageFilter.GaussianBlur(shadow_blur))
    # Set shadow color to dark gray
    shadow_arr = np.array(shadow)
    shadow_arr[:, :, :3] = 100
    shadow_arr[:, :, 3] = (shadow_arr[:, :, 3] * shadow_opacity / 255).astype(np.uint8)
    shadow = Image.fromarray(shadow_arr)
    # Paste: shadow first, then image
    canvas.paste(shadow, (20 + shadow_offset[0], 20 + shadow_offset[1]), shadow)
    canvas.paste(img, (20, 20), img)
    # Export as JPEG
    result = Image.new("RGB", canvas.size, (255, 255, 255))
    result.paste(canvas, mask=canvas.split()[3])
    output = io.BytesIO()
    result.save(output, format="JPEG", quality=90)
    return output.getvalue()


# ============================================================
# Step 5: Resize to max 2400px
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
    Full pipeline: Quality → remove.bg → Result Check → Shadow → Resize.
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

    # Rate limit protection: 0.5s between API calls
    time.sleep(0.5)

    # 3. Result check
    ok, reason = check_result(result)
    if not ok:
        return None, f"SKIP Ergebnis: {reason}"

    # 4. Shadow
    result = add_shadow(result)

    # 5. Resize
    result = resize_final(result)

    return result, "OK"
