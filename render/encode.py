"""Image encoding variants: raw PNG, palette PNG8, WebP lossless, WebP lossy, and webp8 (the PNG8 pixels in a
lossless WebP file)."""

from __future__ import annotations

import io
import time

from PIL import Image

FORMATS = ["png", "png8", "webp_lossless", "webp_q", "webp8"]
EXT = {"png": "png", "png8": "png", "webp_lossless": "webp", "webp_q": "webp", "webp8": "webp"}


def encode(png: bytes, fmt: str, colors: int = 256, webp_quality: int = 85) -> bytes:
    if fmt == "png":
        return png
    im = Image.open(io.BytesIO(png))
    has_alpha = im.mode == "RGBA" and im.getextrema()[3][0] < 255
    src = im if has_alpha else im.convert("RGB")
    buf = io.BytesIO()
    if fmt == "webp8":
        # 2026-11-27: the same 256-colour pixels as png8, stored as lossless WebP: 14 % smaller (measured on 52
        # charts of AIFS 2026112700, +24 h / +240 h: 5.58 -> 4.81 MB, decoded pixels identical)
        q = src.quantize(colors=colors, method=Image.Quantize.FASTOCTREE)
        q.convert("RGBA" if has_alpha else "RGB").save(buf, format="WEBP", lossless=True, method=4)
    elif fmt == "png8":
        # FASTOCTREE also for opaque charts (2026-11-24): MEDIANCUT merged colour-bar bands that cover few pixels
        # (theta-e 333-345 K came out as one or two colours), and was 15 % larger (16 charts of IFS +72 h, measured).
        src.quantize(colors=colors, method=Image.Quantize.FASTOCTREE).save(buf, format="PNG", optimize=True)
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


def with_ext(path: str, fmt: str) -> str:
    """site.path ends in .png; the file of format fmt (webp8 -> .webp)."""
    stem, dot, _ = path.rpartition(".")
    return f"{stem}.{EXT[fmt]}" if dot else path
