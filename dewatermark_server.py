# -*- coding: utf-8 -*-
"""Remove Google Flow's visible sparkle watermark — server-side.

The mark is a fixed-position translucent white glyph alpha-composited over the
render:

    out = img*(1-a) + 255*a      ->      img = (out - 255*a) / (1-a)

`a` was derived offline from the per-pixel minimum across many renders (the
glyph is identical in all of them while the scene varies, so min(out) tends to
255*a wherever some render is dark beneath it). It is embedded below as a tiny
base64 PNG, so this module needs only Pillow: no numpy, no corpus, no state.

The work is a 64x68 pixel region — about 4.4k pixels, sub-millisecond in pure
Python — so it costs effectively nothing per image.
"""
from __future__ import annotations

import base64
import io
import os

# 64x68 alpha mask (0-255) for the glyph region
_ALPHA_B64 = (
    "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"
    "+Tv7zv4P7T8+ygbGtt2k6wAAAABJRU5ErkJggg=="
)

BOX = (1246, 638, 1310, 706)          # x0, y0, x1, y1 in a 1376x768 render
SIZE = (1376, 768)

_mask_cache = None


def disabled() -> bool:
    return os.environ.get("FLOW_DEWATERMARK", "1").strip() in ("0", "off",
                                                               "false", "no")


def available() -> bool:
    """True when we can actually remove the mark (Pillow present, not off)."""
    if disabled():
        return False
    try:
        import PIL  # noqa: F401
        return True
    except Exception:
        return False


def _mask():
    global _mask_cache
    if _mask_cache is None:
        from PIL import Image
        im = Image.open(io.BytesIO(base64.b64decode(_ALPHA_B64))).convert("L")
        _mask_cache = (list(im.getdata()), im.size[0])
    return _mask_cache


def clean_bytes(blob: bytes, quality: int = 85) -> bytes:
    """Strip the watermark from image bytes.

    Returns the input untouched when Pillow is unavailable, the config disables
    it, or the image is not a 1376x768 render (i.e. not one of ours). Never
    raises: a failure here must not cost the caller its image.
    """
    if not blob or not available():
        return blob
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(blob))
        if im.size != SIZE:
            return blob
        im = im.convert("RGB")
        px = im.load()
        alpha, mw = _mask()
        x0, y0, x1, y1 = BOX
        for yy in range(y1 - y0):
            row = yy * mw
            for xx in range(x1 - x0):
                a = alpha[row + xx] / 255.0
                if a <= 0.02:
                    continue
                inv = 1.0 - a
                if inv <= 0.02:
                    continue
                r, g, b = px[x0 + xx, y0 + yy]
                px[x0 + xx, y0 + yy] = (
                    min(255, max(0, int((r - 255.0 * a) / inv + 0.5))),
                    min(255, max(0, int((g - 255.0 * a) / inv + 0.5))),
                    min(255, max(0, int((b - 255.0 * a) / inv + 0.5))),
                )
        out = io.BytesIO()
        # 4:2:0 at q90 lands close to Flow's own encoded size. 4:4:4/q95 looked
        # marginally cleaner but doubled the payload (382 KB vs 187 KB), which
        # is a bad trade for bytes nobody looks at twice.
        im.save(out, format="JPEG", quality=quality, subsampling=2)
        return out.getvalue()
    except Exception:
        return blob
