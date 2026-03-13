"""Image processing via self-hosted rembg on Railway.
Pipeline: Quality Check → Prepare → rembg (PNG+Alpha) → Edge Cleanup → Result Check → Shadow → Crop/Center → Resize
"""
import io
import logging
import time
import os

import numpy as np
import requests
from PIL import Image, ImageFilter, ImageOps, ImageEnhance

log = logging.getLogger("whitebg.processor")

REMBG_URL = os.environ.get("REMBG_URL", "https://rembg-new-production.up.railway.app")


# ============================================================
# Step 1: Quality Check BEFORE API (free)
# ============================================================
def check_quality(image_bytes: bytes) -> tuple:
    """Check blur + brightness. Returns (ok, reason)."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    gray = img.convert("L")
    arr = np.array(gray, dtype=float)
    laplacian = np.abs(np.diff(np.diff(arr, axis=0), axis=0)).mean()
    if laplacian < 1.0:
        return False, f"Zu unscharf (Score: {laplacian:.1f})"
    brightness = np.array(img).mean()
    if brightness < 30:
        return False, f"Zu dunkel (Helligkeit: {brightness:.0f})"
    return True, "OK"


# ============================================================
# Step 1b: Prepare image (auto-contrast + sharpening + resize)
# ============================================================
def prepare_image(image_bytes: bytes) -> bytes:
    """Auto-Kontrast + Schärfung + Resize auf max 1500px VOR rembg.
    Verbessert Objekt/HG-Trennung deutlich bei dunklen Handyfotos."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    img = ImageOps.autocontrast(img, cutoff=1)
    img = ImageEnhance.Sharpness(img).enhance(1.3)
    if max(img.size) > 1500:
        img.thumbnail((1500, 1500), Image.LANCZOS)
    output = io.BytesIO()
    img.save(output, format="JPEG", quality=95)
    return output.getvalue()


# ============================================================
# Step 2: rembg API (self-hosted, free) — PNG for alpha channel
# ============================================================
def remove_background(image_bytes: bytes, max_retries: int = 3) -> bytes:
    """Call self-hosted rembg, returns PNG with alpha channel."""
    url = f"{REMBG_URL}/remove-bg"
    params = {
        "model": "isnet-general-use",
        "format": "png",
        "post_process_mask": "true",
    }
    for attempt in range(max_retries + 1):
        try:
            response = requests.post(
                url,
                params=params,
                files={"image": ("image.jpg", image_bytes, "image/jpeg")},
                timeout=90,
            )
            if response.status_code == 200:
                return response.content
            if response.status_code == 503 and attempt < max_retries:
                wait = 10 * (attempt + 1)
                log.warning(f"  rembg 503, retry in {wait}s (attempt {attempt+1}/{max_retries})")
                time.sleep(wait)
                continue
            raise Exception(f"rembg Fehler: {response.status_code} {response.text[:200]}")
        except requests.exceptions.Timeout:
            if attempt < max_retries:
                log.warning(f"  rembg timeout, retry (attempt {attempt+1}/{max_retries})")
                time.sleep(5)
                continue
            raise Exception("rembg: timeout after all retries")
    raise Exception("rembg: max retries exceeded")


# ============================================================
# Step 2b: Edge Cleanup (median filter on alpha channel)
# ============================================================
def cleanup_edges(png_bytes: bytes) -> bytes:
    """Glättet Alpha-Kanal per Median-Filter, entfernt Artefakt-Pixel an Kanten."""
    img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    r, g, b, a = img.split()
    a = a.filter(ImageFilter.MedianFilter(size=3))
    img = Image.merge("RGBA", (r, g, b, a))
    output = io.BytesIO()
    img.save(output, format="PNG")
    return output.getvalue()


# ============================================================
# Step 3: Result Check (too much removed?)
# ============================================================
def check_result(png_bytes: bytes) -> tuple:
    """Check if API removed too much (>92% transparent)."""
    img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    alpha = np.array(img.split()[3])
    transparent_ratio = (alpha < 15).mean()
    if transparent_ratio > 0.92:
        return False, f"API hat zu viel entfernt ({transparent_ratio*100:.0f}% transparent)"
    return True, "OK"


# ============================================================
# Step 4: Soft Drop Shadow (Pillow-based)
# ============================================================
def add_shadow(png_bytes: bytes) -> bytes:
    """Weicher Schlagschatten unter dem Objekt, arbeitet auf RGBA PNG."""
    img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    alpha = img.split()[3]
    w, h = img.size

    shadow_offset = (6, 10)
    shadow_blur = 15
    shadow_opacity = 50

    shadow = Image.new("RGBA", (w + 40, h + 40), (0, 0, 0, 0))
    shadow_alpha = alpha.copy()
    shadow_layer = Image.new("RGBA", alpha.size, (0, 0, 0, 0))
    shadow_layer.putalpha(shadow_alpha)
    shadow.paste(shadow_layer, (20 + shadow_offset[0], 20 + shadow_offset[1]))
    shadow = shadow.filter(ImageFilter.GaussianBlur(shadow_blur))

    shadow_arr = np.array(shadow)
    shadow_arr[:, :, :3] = 80
    shadow_arr[:, :, 3] = (shadow_arr[:, :, 3].astype(float) * shadow_opacity / 100).astype(np.uint8)
    shadow = Image.fromarray(shadow_arr)

    canvas = Image.new("RGBA", (w + 40, h + 40), (255, 255, 255, 255))
    canvas = Image.alpha_composite(canvas, shadow)
    canvas.paste(img, (20, 20), img)

    output = io.BytesIO()
    canvas.save(output, format="PNG")
    return output.getvalue()


# ============================================================
# Step 5: Crop + Center on white canvas
# ============================================================
def crop_and_center(png_bytes: bytes) -> bytes:
    """Crop to content bounding box via alpha, center on white canvas with 5% margin."""
    img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    alpha = np.array(img.split()[3])

    rows = np.any(alpha > 15, axis=1)
    cols = np.any(alpha > 15, axis=0)

    if not rows.any() or not cols.any():
        result = Image.new("RGB", img.size, (255, 255, 255))
        output = io.BytesIO()
        result.save(output, format="JPEG", quality=92)
        return output.getvalue()

    y_min, y_max = np.where(rows)[0][[0, -1]]
    x_min, x_max = np.where(cols)[0][[0, -1]]

    cropped = img.crop((x_min, y_min, x_max + 1, y_max + 1))
    cw, ch = cropped.size

    margin_x = int(cw * 0.05)
    margin_y = int(ch * 0.05)
    canvas_w = cw + 2 * margin_x
    canvas_h = ch + 2 * margin_y

    canvas = Image.new("RGBA", (canvas_w, canvas_h), (255, 255, 255, 255))
    canvas.paste(cropped, (margin_x, margin_y), cropped)

    result = Image.new("RGB", canvas.size, (255, 255, 255))
    result.paste(canvas, mask=canvas.split()[3])

    output = io.BytesIO()
    result.save(output, format="JPEG", quality=92)
    return output.getvalue()


# ============================================================
# Step 6: Resize to max 2400px
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
def process_image(image_bytes: bytes, filename: str, rembg_url: str = "") -> tuple:
    """
    Full pipeline: Quality → Prepare → rembg (PNG) → Edge Cleanup → Result Check
    → Shadow → Crop/Center → Resize.
    Returns (result_bytes | None, status_message).
    """
    # 1. Quality check
    ok, reason = check_quality(image_bytes)
    if not ok:
        return None, f"SKIP Qualit\u00e4t: {reason}"

    # 1b. Prepare (auto-contrast + sharpening + resize to 1500px)
    prepared = prepare_image(image_bytes)

    # 2. rembg API (free, self-hosted) — returns PNG with alpha
    try:
        result = remove_background(prepared)
    except Exception as e:
        return None, f"FEHLER API: {e}"

    # 2b. Edge cleanup (median filter on alpha channel)
    result = cleanup_edges(result)

    # 3. Result check (on alpha channel)
    ok, reason = check_result(result)
    if not ok:
        return None, f"SKIP Ergebnis: {reason}"

    # 4. Soft drop shadow
    result = add_shadow(result)

    # 5. Crop + Center (converts RGBA → JPEG with white BG)
    result = crop_and_center(result)

    # 6. Resize
    result = resize_final(result)

    return result, "OK"
