"""Image post-processing: blank detection and token-friendly encoding."""

from __future__ import annotations

import io

from PIL import Image, ImageStat

# Flat UI screenshots are often smaller as PNG than as JPEG. Pick whichever
# is smaller so we spend fewer tokens without guessing.
DEFAULT_MAX_SIDE = 1600
DEFAULT_QUALITY = 80


def analyse(image: Image.Image) -> dict:
    grey = image.convert("L")
    stat = ImageStat.Stat(grey)
    low, high = grey.getextrema()
    return {
        "size": list(image.size),
        "mean": round(stat.mean[0], 1),
        "stddev": round(stat.stddev[0], 2),
        "extrema": [low, high],
        "blank": (high - low) < 8,
    }


def _downscale(image: Image.Image, max_side: int) -> Image.Image:
    if max_side <= 0:
        return image
    longest = max(image.size)
    if longest <= max_side:
        return image
    scale = max_side / longest
    size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    return image.resize(size, Image.LANCZOS)


def encode(
    image: Image.Image, max_side: int = DEFAULT_MAX_SIDE, quality: int = DEFAULT_QUALITY
) -> tuple[bytes, str]:
    """Return (bytes, format) with the smaller of PNG and JPEG."""
    image = _downscale(image, max_side)

    png_buffer = io.BytesIO()
    image.save(png_buffer, format="PNG", optimize=True)
    png = png_buffer.getvalue()

    jpeg_buffer = io.BytesIO()
    image.convert("RGB").save(
        jpeg_buffer, format="JPEG", quality=quality, optimize=True, subsampling=0
    )
    jpeg = jpeg_buffer.getvalue()

    if len(png) <= len(jpeg):
        return png, "png"
    return jpeg, "jpeg"
