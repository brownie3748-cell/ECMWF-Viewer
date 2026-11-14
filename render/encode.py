"""Image encoding variants: raw PNG, palette PNG8, WebP lossless, WebP lossy."""

from __future__ import annotations

import io
import time

from PIL import Image

FORMATS = ["png", "png8", "webp_lossless", "webp_q"]
EXT = {"png": "png", "png8": "png", "webp_lossless": "webp", "webp_q": "webp"}


def encode(png: bytes, fmt: str, colors: int = 256, webp_quality: int = 85) -> bytes:
    if fmt == "png":
        return png
    im = Image.open(io.BytesIO(png))
    has_alpha = im.mode == "RGBA" and im.getextrema()[3][0] < 255
    src = im if has_alpha else im.convert("RGB")
    buf = io.BytesIO()
    if fmt == "png8":
        method = Image.Quantize.FASTOCTREE if has_alpha else Image.Quantize.MEDIANCUT
        src.quantize(colors=colors, method=method).save(buf, format="PNG", optimize=True)
    elif fmt == "webp_lossless":
        src.save(buf, format="WEBP", lossless=True, method=4)
    elif fmt == "webp_q":
        src.save(buf, format="WEBP", quality=webp_quality, method=4)
    else:
        raise ValueError(fmt)
    return buf.getvalue()


def variants(png: bytes, colors: int = 256, webp_quality: int = 85):
    """Return ({fmt: bytes}, {fmt: seconds})."""
    out, secs = {}, {}
    for f in FORMATS:
        t0 = time.monotonic()
        out[f] = encode(png, f, colors, webp_quality)
        secs[f] = time.monotonic() - t0
    return out, secs
