"""
Image processing: background removal with rembg + white background compositing.
Includes ONNX thread limiting and memory cleanup.
"""
import gc
import io
import os
import logging

import onnxruntime as ort
from PIL import Image
from rembg import new_session, remove

log = logging.getLogger("whitebg.processor")


def build_session(model_name: str):
    """Build rembg session with ONNX thread limits for Railway CPU protection."""
    ort_threads = int(os.getenv("ORT_THREADS", "2"))

    sess_options = ort.SessionOptions()
    sess_options.intra_op_num_threads = ort_threads
    sess_options.inter_op_num_threads = 1
    sess_options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    # Disable spin-waiting = less CPU idle load
    sess_options.add_session_config_entry("session.intra_op.allow_spinning", "0")

    log.info(f"Building rembg session: model={model_name}, ort_threads={ort_threads}")
    session = new_session(model_name)
    log.info(f"Model {model_name} loaded successfully")
    return session


def process_image(image_bytes: bytes, session, max_size: int = 2400, jpeg_quality: int = 90) -> bytes:
    """
    Remove background from image and composite onto white background.

    Steps:
    1. Open image, resize to max_size BEFORE rembg (important for BiRefNet)
    2. Run rembg.remove() -> RGBA with transparent BG
    3. Composite onto white #FFFFFF background
    4. Export as JPEG at specified quality
    5. Cleanup memory (gc.collect)
    """
    img = Image.open(io.BytesIO(image_bytes))
    original_size = img.size
    log.info(f"  Input: {img.size[0]}x{img.size[1]}, mode={img.mode}")

    # Step 1: Resize BEFORE rembg (BiRefNet trained on 1024x1024, large input = more RAM)
    w, h = img.size
    if max(w, h) > max_size:
        ratio = max_size / max(w, h)
        new_w, new_h = int(w * ratio), int(h * ratio)
        img = img.resize((new_w, new_h), Image.LANCZOS)
        log.info(f"  Resized: {original_size[0]}x{original_size[1]} -> {new_w}x{new_h}")

    # Convert to RGB for consistent input
    if img.mode != "RGB":
        img = img.convert("RGB")

    # Step 2: Remove background
    input_buf = io.BytesIO()
    img.save(input_buf, format="PNG")
    input_buf.seek(0)
    input_bytes_png = input_buf.getvalue()

    result_bytes = remove(input_bytes_png, session=session)
    result_img = Image.open(io.BytesIO(result_bytes)).convert("RGBA")

    # Step 3: Composite onto white background
    white_bg = Image.new("RGBA", result_img.size, (255, 255, 255, 255))
    white_bg.paste(result_img, mask=result_img.split()[3])
    final = white_bg.convert("RGB")

    # Step 4: Export as JPEG
    output_buf = io.BytesIO()
    final.save(output_buf, format="JPEG", quality=jpeg_quality, optimize=True)
    output_buf.seek(0)
    result = output_buf.getvalue()

    log.info(f"  Output: {final.size[0]}x{final.size[1]}, {len(result)} bytes JPEG")

    # Step 5: Explicit memory cleanup (onnxruntime RAM leak with varying image sizes)
    del img, input_buf, input_bytes_png, result_bytes, result_img, white_bg, final, output_buf
    gc.collect()

    return result
