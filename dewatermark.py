"""Remove Google Flow's visible sparkle watermark.

The mark is a fixed-position, translucent white glyph alpha-composited over the
render:
      out = img*(1-a) + 255*a      ->   img = (out - 255*a) / (1-a)
Recovering `a` needs a sample where the underlying image is black, which a
single image cannot guarantee. Instead use the per-pixel MINIMUM across many
renders: the glyph is identical in all of them while the scene varies, so
min(out) tends to 255*a wherever at least one image is dark under the glyph.

Usage:
  python dewatermark.py                 # clean every output, write clean_*.jpg
  python dewatermark.py in.jpg out.jpg  # one file
  python dewatermark.py --alpha a.png   # dump the derived mask for inspection
"""
import glob
import os
import sys
import numpy as np
from PIL import Image

OUT_DIR = os.path.dirname(os.path.abspath(__file__)) + "/outputs"
# glyph region (x0, y0, x1, y1) in 1376x768 renders, padded around the sparkle
BOX = (1246, 638, 1310, 706)
SIZE = (1376, 768)


def build_alpha(paths=None, save=None):
    paths = paths or sorted(glob.glob(os.path.join(OUT_DIR, "*.jpg")))
    paths = [p for p in paths if not os.path.basename(p).startswith("clean_")]
    stack = []
    for p in paths:
        try:
            im = Image.open(p).convert("RGB")
            if im.size != SIZE:
                continue
            stack.append(np.asarray(im.crop(BOX)).astype(np.float32))
        except Exception:
            continue
    if not stack:
        raise SystemExit("no usable images")
    mn = np.minimum.reduce(stack)                 # per-pixel minimum
    a = mn.max(axis=2) / 255.0                    # strongest channel
    a[a < 0.06] = 0.0                             # kill sensor noise
    if save:
        Image.fromarray((a * 255).astype(np.uint8)).save(save)
    print(f"alpha from {len(stack)} images: max={a.max():.2f} "
          f"pixels>{0.06}={int((a > 0.06).sum())}")
    return a


def clean(path, out_path, alpha):
    im = Image.open(path).convert("RGB")
    if im.size != SIZE:
        raise SystemExit(f"unexpected size {im.size}")
    arr = np.asarray(im).astype(np.float32)
    x0, y0, x1, y1 = BOX
    region = arr[y0:y1, x0:x1, :]
    a3 = alpha[..., None]
    fixed = (region - 255.0 * a3) / np.maximum(1.0 - a3, 1e-3)
    arr[y0:y1, x0:x1, :] = np.clip(fixed, 0, 255)
    Image.fromarray(arr.astype(np.uint8)).save(out_path, quality=96)
    return out_path


def pair_png(src, dst, tag):
    """before | after, magnified, for visual checking."""
    b = (BOX[0] - 14, BOX[1] - 14, BOX[2] + 14, BOX[3] + 14)
    a0 = Image.open(src).convert("RGB").crop(b)
    a1 = Image.open(dst).convert("RGB").crop(b)
    out = Image.new("RGB", (a0.width * 2 + 8, a0.height), (255, 0, 0))
    out.paste(a0, (0, 0))
    out.paste(a1, (a0.width + 8, 0))
    out = out.resize((out.width * 5, out.height * 5), Image.NEAREST)
    p = f"/tmp/wm_pair_{tag}.png"
    out.save(p)
    return p


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--alpha" in sys.argv:
        idx = sys.argv.index("--alpha")
        dst = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else "/tmp/wm_alpha.png"
        build_alpha(save=dst)
        # show it big, on grey, so the shape is obvious
        im = Image.open(dst).convert("L").resize((BOX[2] - BOX[0]) * 6 and
                                                 ((BOX[2] - BOX[0]) * 6, (BOX[3] - BOX[1]) * 6),
                                                 Image.NEAREST)
        im.save("/tmp/wm_alpha_big.png")
        print("mask ->", dst, "| enlarged -> /tmp/wm_alpha_big.png")
        return
    alpha = build_alpha()
    if len(args) == 2:
        clean(args[0], args[1], alpha)
        print("cleaned ->", args[1])
        return
    files = [p for p in sorted(glob.glob(os.path.join(OUT_DIR, "*.jpg")))
             if not os.path.basename(p).startswith("clean_")]
    for p in files:
        dst = os.path.join(OUT_DIR, "clean_" + os.path.basename(p))
        clean(p, dst, alpha)
        tag = os.path.basename(p).split(".")[0][-8:]
        print(f"  {os.path.basename(p):34} -> clean_  {pair_png(p, dst, tag)}")


if __name__ == "__main__":
    main()
