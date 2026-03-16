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
from scipy import ndimage

log = logging.getLogger("whitebg.processor")

REMBG_URL = os.environ.get("REMBG_URL", "https://rembg-new-production.up.railway.app")


# ============================================================
# Step 1: Quality Check BEFORE API (free)
# ============================================================
SKIP_PATTERNS = ("_attr", "_bkgrd", "_weiss", "_Photoroom", "-Photoroom")

MIN_IMAGE_SIZE = 200   # px — Bilder kleiner als 200x200 überspringen
MIN_FILE_SIZE = 20480  # 20 KB — zu kleine Dateien (Thumbnails) überspringen

def check_quality(image_bytes: bytes, filename: str = "") -> tuple:
    """Check filename patterns, file size, dimensions, blur + brightness. Returns (ok, reason)."""
    if any(p in filename for p in SKIP_PATTERNS):
        return False, f"Dateiname-Skip ({filename})"
    if len(image_bytes) < MIN_FILE_SIZE:
        return False, f"Datei zu klein ({len(image_bytes):,} bytes < {MIN_FILE_SIZE:,})"
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    w, h = img.size
    if w < MIN_IMAGE_SIZE or h < MIN_IMAGE_SIZE:
        return False, f"Bild zu klein ({w}x{h} < {MIN_IMAGE_SIZE}x{MIN_IMAGE_SIZE})"
    gray = img.convert("L")
    arr = np.array(gray, dtype=float)
    laplacian = np.abs(np.diff(np.diff(arr, axis=0), axis=0)).mean()
    if laplacian < 0.3:
        return False, f"Zu unscharf (Score: {laplacian:.1f})"
    brightness = np.array(img).mean()
    if brightness < 30:
        return False, f"Zu dunkel (Helligkeit: {brightness:.0f})"
    return True, "OK"


# ============================================================
# Step 1b: Prepare image (auto-contrast + sharpening + resize)
# ============================================================
def prepare_image(image_bytes: bytes) -> bytes:
    """Auto-Kontrast + Schärfung + Resize auf max 1024px VOR rembg.
    BiRefNet wurde auf 1024x1024 trainiert — größere Inputs geben schlechtere Ergebnisse."""
    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")
    img = ImageOps.autocontrast(img, cutoff=1)
    img = ImageEnhance.Sharpness(img).enhance(1.3)
    if max(img.size) > 1024:
        img.thumbnail((1024, 1024), Image.LANCZOS)
    output = io.BytesIO()
    img.save(output, format="JPEG", quality=95)
    return output.getvalue()


# ============================================================
# Step 2: rembg API (self-hosted, free) — PNG for alpha channel
# ============================================================
def remove_background(image_bytes: bytes, max_retries: int = 3, model: str = "birefnet-general-lite") -> bytes:
    """Call self-hosted rembg, returns PNG with alpha channel."""
    url = f"{REMBG_URL}/remove-bg"
    params = {
        "model": model,
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
            if response.status_code in (502, 503) and attempt < max_retries:
                wait = 15 * (attempt + 1)
                log.warning(f"  rembg {response.status_code}, retry in {wait}s (attempt {attempt+1}/{max_retries})")
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
# Step 2b: Edge Cleanup (morphological + feathering)
# ============================================================
def cleanup_edges(png_bytes: bytes) -> bytes:
    """Morphological Close + leichte Erosion + Edge-Feathering auf Alpha-Kanal."""
    img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    r, g, b, a = img.split()
    alpha_arr = np.array(a, dtype=np.uint8)

    binary = (alpha_arr > 15).astype(np.uint8)

    # 1. Morphological Close: kleine Löcher in der Maske füllen (Dilate → Erode)
    struct = ndimage.generate_binary_structure(2, 1)
    closed = ndimage.binary_closing(binary, structure=struct, iterations=2).astype(np.uint8)

    # 2. Leichte Erosion: Halo-Effekt am Rand entfernen (1px)
    eroded = ndimage.binary_erosion(closed, structure=struct, iterations=1).astype(np.uint8)

    # 3. Edge-Feathering: Gaussian Blur NUR an den Kanten (weiche Übergänge)
    edge = closed.astype(float) - eroded.astype(float)
    feathered = ndimage.gaussian_filter(eroded.astype(float), sigma=0.8)
    feathered = np.clip(feathered + edge * 0.5, 0, 1)

    alpha_arr = (feathered * 255).astype(np.uint8)
    alpha_arr = np.where(feathered > 0.5, np.maximum(alpha_arr, np.array(a)), alpha_arr)

    a = Image.fromarray(alpha_arr)
    img = Image.merge("RGBA", (r, g, b, a))
    output = io.BytesIO()
    img.save(output, format="PNG")
    return output.getvalue()


# ============================================================
# Step 2c: Keep only largest connected component in alpha
# ============================================================
def keep_largest_component(png_bytes: bytes) -> bytes:
    """Behält nur die größte zusammenhängende Region im Alpha-Kanal.
    Entfernt kleine Artefakte und isolierte Pixel-Inseln."""
    img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    r, g, b, a = img.split()
    alpha_arr = np.array(a)

    binary = (alpha_arr > 15).astype(np.uint8)
    labeled, num_features = ndimage.label(binary)

    if num_features <= 1:
        return png_bytes

    sizes = ndimage.sum(binary, labeled, range(1, num_features + 1))
    largest_label = np.argmax(sizes) + 1
    mask = (labeled == largest_label)
    alpha_arr[~mask] = 0

    a = Image.fromarray(alpha_arr)
    img = Image.merge("RGBA", (r, g, b, a))
    output = io.BytesIO()
    img.save(output, format="PNG")
    return output.getvalue()


# ============================================================
# Step 2d: Auto White Balance on foreground object only
# ============================================================
def auto_white_balance(png_bytes: bytes) -> bytes:
    """Korrigiert Farbstich (Gelb/Blau) NUR am Objekt, nicht am transparenten HG.
    Analysiert opake Pixel, verschiebt Kanäle Richtung Neutralgrau."""
    img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    r, g, b, a = img.split()
    alpha_arr = np.array(a)
    mask = alpha_arr > 128

    if mask.sum() < 100:
        return png_bytes

    r_arr = np.array(r, dtype=float)
    g_arr = np.array(g, dtype=float)
    b_arr = np.array(b, dtype=float)

    r_mean = r_arr[mask].mean()
    g_mean = g_arr[mask].mean()
    b_mean = b_arr[mask].mean()
    gray_mean = (r_mean + g_mean + b_mean) / 3.0

    if gray_mean < 1:
        return png_bytes

    r_scale = gray_mean / max(r_mean, 1)
    g_scale = gray_mean / max(g_mean, 1)
    b_scale = gray_mean / max(b_mean, 1)

    max_shift = 1.15
    r_scale = np.clip(r_scale, 1 / max_shift, max_shift)
    g_scale = np.clip(g_scale, 1 / max_shift, max_shift)
    b_scale = np.clip(b_scale, 1 / max_shift, max_shift)

    r_arr = np.clip(r_arr * r_scale, 0, 255).astype(np.uint8)
    g_arr = np.clip(g_arr * g_scale, 0, 255).astype(np.uint8)
    b_arr = np.clip(b_arr * b_scale, 0, 255).astype(np.uint8)

    r = Image.fromarray(r_arr)
    g = Image.fromarray(g_arr)
    b = Image.fromarray(b_arr)
    img = Image.merge("RGBA", (r, g, b, a))

    output = io.BytesIO()
    img.save(output, format="PNG")
    return output.getvalue()


# ============================================================
# Step 2e: Color Despill — Farbsäume an Kanten entfernen
# ============================================================
def color_despill(png_bytes: bytes) -> bytes:
    """Korrigiert Farbkontamination an Kanten (wo Alpha 10-200).
    Ersetzt Kantenfarbe durch die Durchschnittsfarbe des Objekt-Inneren."""
    img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    r, g, b, a = img.split()
    alpha_arr = np.array(a)
    r_arr = np.array(r, dtype=float)
    g_arr = np.array(g, dtype=float)
    b_arr = np.array(b, dtype=float)

    interior = alpha_arr > 200
    edge = (alpha_arr > 10) & (alpha_arr <= 200)

    if interior.sum() < 100 or edge.sum() < 10:
        return png_bytes

    r_int = r_arr[interior].mean()
    g_int = g_arr[interior].mean()
    b_int = b_arr[interior].mean()

    blend = (alpha_arr[edge].astype(float) - 10) / 190.0
    r_arr[edge] = r_arr[edge] * blend + r_int * (1 - blend)
    g_arr[edge] = g_arr[edge] * blend + g_int * (1 - blend)
    b_arr[edge] = b_arr[edge] * blend + b_int * (1 - blend)

    r = Image.fromarray(np.clip(r_arr, 0, 255).astype(np.uint8))
    g = Image.fromarray(np.clip(g_arr, 0, 255).astype(np.uint8))
    b = Image.fromarray(np.clip(b_arr, 0, 255).astype(np.uint8))
    img = Image.merge("RGBA", (r, g, b, a))

    output = io.BytesIO()
    img.save(output, format="PNG")
    return output.getvalue()


# ============================================================
# Step 2f: Gamma-Korrektur für dunkle Möbel
# ============================================================
def gamma_correct(png_bytes: bytes) -> bytes:
    """Hebt dunkle Objekte leicht an (Gamma 1.2–1.4) damit Details auf weißem HG sichtbar bleiben.
    Nur auf Vordergrund-Pixel, nur wenn Objekt dunkel (mean < 100)."""
    img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    r, g, b, a = img.split()
    alpha_arr = np.array(a)
    mask = alpha_arr > 128

    if mask.sum() < 100:
        return png_bytes

    r_arr = np.array(r, dtype=float)
    g_arr = np.array(g, dtype=float)
    b_arr = np.array(b, dtype=float)

    obj_mean = (r_arr[mask].mean() + g_arr[mask].mean() + b_arr[mask].mean()) / 3.0

    if obj_mean >= 100:
        return png_bytes

    gamma = 1.2 if obj_mean > 60 else 1.4
    lut = np.array([((i / 255.0) ** (1.0 / gamma)) * 255 for i in range(256)], dtype=np.uint8)

    r_arr = lut[np.array(r)]
    g_arr = lut[np.array(g)]
    b_arr = lut[np.array(b)]

    r_arr[~mask] = np.array(r)[~mask]
    g_arr[~mask] = np.array(g)[~mask]
    b_arr[~mask] = np.array(b)[~mask]

    r = Image.fromarray(r_arr)
    g = Image.fromarray(g_arr)
    b = Image.fromarray(b_arr)
    img = Image.merge("RGBA", (r, g, b, a))

    output = io.BytesIO()
    img.save(output, format="PNG")
    return output.getvalue()


# ============================================================
# Step 3: Result Check (too much removed?)
# ============================================================
def check_result(png_bytes: bytes) -> tuple:
    """Check if API removed too much (>92% transparent). Logs foreground ratio."""
    img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    alpha = np.array(img.split()[3])
    transparent_ratio = (alpha < 15).mean()
    fg_ratio = 1.0 - transparent_ratio
    if transparent_ratio > 0.92:
        return False, f"API hat zu viel entfernt ({transparent_ratio*100:.0f}% transparent)"
    if fg_ratio < 0.15:
        log.warning(f"  ⚠️ Objekt sehr klein ({fg_ratio*100:.0f}% Vordergrund) — möglicherweise falsches Objekt erkannt")
    elif fg_ratio > 0.85:
        log.warning(f"  ⚠️ Fast nichts entfernt ({fg_ratio*100:.0f}% Vordergrund) — HG-Entfernung evtl. fehlgeschlagen")
    else:
        log.info(f"  Vordergrund: {fg_ratio*100:.0f}%")
    return True, "OK"


# ============================================================
# Step 4: Floor Shadow + White BG (realistic furniture shadow)
# ============================================================
def add_shadow(png_bytes: bytes) -> bytes:
    """Realistischer Boden-Schatten (direkt unter Objekt, breiter als tief).
    Kein diagonaler Drop-Shadow — professioneller Möbelfotografie-Look.
    Input: RGBA PNG, Output: JPEG."""
    img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    alpha = img.split()[3]
    w, h = img.size

    pad = 40
    shadow_blur_x = 20
    shadow_blur_y = 8
    shadow_opacity = 40
    shadow_drop = 6

    shadow = Image.new("L", (w + pad, h + pad), 0)
    shadow_alpha = alpha.copy()
    shadow.paste(shadow_alpha, (pad // 2, pad // 2 + shadow_drop))

    shadow_arr = np.array(shadow, dtype=float)
    shadow_arr = ndimage.gaussian_filter(shadow_arr, sigma=[shadow_blur_y, shadow_blur_x])

    shadow_arr = np.clip(shadow_arr * shadow_opacity / 100, 0, 255).astype(np.uint8)

    shadow_rgba = Image.new("RGBA", (w + pad, h + pad), (0, 0, 0, 0))
    shadow_layer = Image.new("RGBA", shadow_rgba.size, (60, 60, 60, 0))
    shadow_layer.putalpha(Image.fromarray(shadow_arr))

    canvas = Image.new("RGBA", (w + pad, h + pad), (255, 255, 255, 255))
    canvas = Image.alpha_composite(canvas, shadow_layer)
    canvas.paste(img, (pad // 2, pad // 2), img)

    result = Image.new("RGB", canvas.size, (255, 255, 255))
    result.paste(canvas, mask=canvas.split()[3])

    output = io.BytesIO()
    result.save(output, format="JPEG", quality=92)
    return output.getvalue()


# ============================================================
# Step 5: Crop + Center on transparent canvas (keeps RGBA)
# ============================================================
def crop_and_center(png_bytes: bytes) -> bytes:
    """Crop to content bounding box, smart padding based on object size. Returns RGBA PNG.
    Small objects get more padding (15%), large objects less (3%)."""
    img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    alpha = np.array(img.split()[3])

    rows = np.any(alpha > 15, axis=1)
    cols = np.any(alpha > 15, axis=0)

    if not rows.any() or not cols.any():
        output = io.BytesIO()
        img.save(output, format="PNG")
        return output.getvalue()

    y_min, y_max = np.where(rows)[0][[0, -1]]
    x_min, x_max = np.where(cols)[0][[0, -1]]

    cropped = img.crop((x_min, y_min, x_max + 1, y_max + 1))
    cw, ch = cropped.size

    img_area = img.size[0] * img.size[1]
    obj_area = cw * ch
    fill_ratio = obj_area / max(img_area, 1)

    if fill_ratio < 0.10:
        margin_pct = 0.15
    elif fill_ratio < 0.30:
        margin_pct = 0.10
    elif fill_ratio < 0.60:
        margin_pct = 0.07
    else:
        margin_pct = 0.03

    margin_x = int(cw * margin_pct)
    margin_y = int(ch * margin_pct)
    canvas_w = cw + 2 * margin_x
    canvas_h = ch + 2 * margin_y

    canvas = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    canvas.paste(cropped, (margin_x, margin_y), cropped)

    output = io.BytesIO()
    canvas.save(output, format="PNG")
    return output.getvalue()


# ============================================================
# Step 6: Resize to square 1:1 (2000x2000px, marketplace-ready)
# ============================================================
def resize_final(image_bytes: bytes, target_size: int = 2000) -> bytes:
    """Pad to square 1:1 or 4:5 portrait canvas, center object, white BG.
    Very tall objects (h > 1.8*w) get 4:5 to avoid looking tiny in 1:1."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    w, h = img.size

    # Aspect ratio guard: tall objects → 4:5 portrait, otherwise 1:1 square
    if h > 1.8 * w:
        canvas_w = target_size
        canvas_h = int(target_size * 5 / 4)
    else:
        canvas_w = target_size
        canvas_h = target_size

    # Shrink to fit inside canvas (never upscale beyond original)
    img.thumbnail((canvas_w, canvas_h), Image.LANCZOS)
    iw, ih = img.size

    # Center on white canvas
    canvas = Image.new("RGB", (canvas_w, canvas_h), (255, 255, 255))
    x = (canvas_w - iw) // 2
    y = (canvas_h - ih) // 2
    canvas.paste(img, (x, y))

    output = io.BytesIO()
    canvas.save(output, format="JPEG", quality=90)
    return output.getvalue()


# ============================================================
# Main pipeline
# ============================================================
def process_image(image_bytes: bytes, filename: str, rembg_url: str = "") -> tuple:
    """
    Full pipeline: Quality → Prepare → rembg (PNG) → Edge Cleanup → Result Check
    → Crop/Center → Shadow+WhiteBG → Resize.
    Returns (result_bytes | None, status_message).
    """
    # 1. Quality check (incl. filename pattern skip)
    ok, reason = check_quality(image_bytes, filename)
    if not ok:
        return None, f"SKIP Qualit\u00e4t: {reason}"

    # 1b. Prepare (auto-contrast + sharpening + resize to 1024px)
    prepared = prepare_image(image_bytes)

    # 2. rembg API (free, self-hosted) — returns PNG with alpha
    try:
        result = remove_background(prepared)
    except Exception as e:
        return None, f"FEHLER API: {e}"

    # Diagnose: FG-Ratio direkt nach rembg (vor jeglicher Post-Verarbeitung)
    _diag_img = Image.open(io.BytesIO(result)).convert("RGBA")
    _diag_alpha = np.array(_diag_img.split()[3])
    _diag_fg = (_diag_alpha > 15).mean()
    log.info(f"  [DIAG] rembg raw FG: {_diag_fg*100:.0f}%  (alpha>15 = Vordergrund)")

    # Fallback: wenn FG > 90% → retry mit isnet-general-use
    if _diag_fg > 0.90:
        log.warning(f"  ⚠️ FG {_diag_fg*100:.0f}% > 90% — Fallback-Modell isnet-general-use")
        try:
            result = remove_background(prepared, model="isnet-general-use")
            _diag_img2 = Image.open(io.BytesIO(result)).convert("RGBA")
            _diag_alpha2 = np.array(_diag_img2.split()[3])
            _diag_fg2 = (_diag_alpha2 > 15).mean()
            log.info(f"  [DIAG] Fallback FG: {_diag_fg2*100:.0f}%")
            if _diag_fg2 < _diag_fg:
                log.info(f"  ✅ Fallback besser ({_diag_fg*100:.0f}% → {_diag_fg2*100:.0f}%)")
            else:
                log.info(f"  ↩️ Fallback nicht besser, behalte Original")
                result = remove_background(prepared)  # re-run original
        except Exception as e:
            log.warning(f"  Fallback fehlgeschlagen: {e} — behalte Original")

    # 2b. Edge cleanup (median filter on alpha channel)
    result = cleanup_edges(result)

    # 2c. Keep only largest foreground region (remove artifact islands)
    result = keep_largest_component(result)

    # 2d. Auto white balance on foreground object (fix color cast)
    result = auto_white_balance(result)

    # 2e. Color despill (fix color bleeding at edges from original background)
    result = color_despill(result)

    # 2f. Gamma correction for dark furniture (lift details on white BG)
    result = gamma_correct(result)

    # 3. Result check (on alpha channel)
    ok, reason = check_result(result)
    if not ok:
        return None, f"SKIP Ergebnis: {reason}"

    # 4. Crop + Center (RGBA → RGBA, erst zentrieren vor Shadow!)
    result = crop_and_center(result)

    # 5. Shadow + White BG (RGBA → JPEG)
    result = add_shadow(result)

    # 6. Resize
    result = resize_final(result)

    return result, "OK"
