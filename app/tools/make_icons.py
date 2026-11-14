"""Regenerate the launcher icons, splash screens and web icon (run from app/: python tools/make_icons.py).

Motif: white curved isobars and a red subtropical-high line on the teal app colour.
Every existing PNG under android/app/src/main/res is redrawn at its current size.
"""

import pathlib

from PIL import Image, ImageDraw

TEAL = (15, 143, 128, 255)
WHITE = (255, 255, 255, 255)
RED = (224, 0, 0, 255)
RES = pathlib.Path("android/app/src/main/res")


def motif(size: int, bg=TEAL, scale=1.0) -> Image.Image:
    """Square image; the motif fills `scale` of the side, centred."""
    ss = 4                                   # supersample for smooth lines
    n = size * ss
    im = Image.new("RGBA", (n, n), bg if bg else (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    m = n * (1 - scale) / 2 + n * scale * 0.12
    w = n * scale * 0.76
    lw = max(2, int(n * scale * 0.055))
    for i, frac in enumerate((0.12, 0.36, 0.60)):          # isobars
        top = m + w * frac
        d.arc([m - w * 0.35, top, m + w * 1.35, top + w * 1.1], 200, 340, fill=WHITE, width=lw)
    top = m + w * 0.84                                       # red WPSH line
    d.arc([m - w * 0.35, top, m + w * 1.35, top + w * 1.1], 205, 335, fill=RED, width=int(lw * 1.2))
    return im.resize((size, size), Image.LANCZOS)


def rounded(im: Image.Image, radius_frac=0.22, circle=False) -> Image.Image:
    s = im.size[0]
    mask = Image.new("L", (s * 4, s * 4), 0)
    dm = ImageDraw.Draw(mask)
    if circle:
        dm.ellipse([0, 0, s * 4 - 1, s * 4 - 1], fill=255)
    else:
        dm.rounded_rectangle([0, 0, s * 4 - 1, s * 4 - 1], radius=int(s * 4 * radius_frac), fill=255)
    out = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    out.paste(im, (0, 0), mask.resize((s, s), Image.LANCZOS))
    return out


def main():
    for p in sorted(RES.glob("*/*.png")):
        w, h = Image.open(p).size
        name = p.name
        if name == "ic_launcher_foreground.png":
            img = motif(w, bg=None, scale=0.62)               # adaptive-icon safe zone
        elif name == "ic_launcher_round.png":
            img = rounded(motif(w), circle=True)
        elif name == "ic_launcher.png":
            img = rounded(motif(w))
        elif name == "splash.png":
            img = Image.new("RGBA", (w, h), TEAL)
            side = int(min(w, h) * 0.38)
            img.alpha_composite(motif(side), ((w - side) // 2, (h - side) // 2))
        else:
            continue
        img.save(p, optimize=True)
        print("wrote", p, img.size)
    (RES / "values/ic_launcher_background.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<resources>\n'
        '    <color name="ic_launcher_background">#0F8F80</color>\n</resources>\n')
    rounded(motif(192)).save("www/icon-192.png", optimize=True)
    print("wrote www/icon-192.png")


if __name__ == "__main__":
    main()
