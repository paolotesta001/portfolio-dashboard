"""Generate app icons (portfolio donut chart) for the Rebalancer PWA."""
from PIL import Image, ImageDraw, ImageChops
import os

OUT = os.path.dirname(os.path.abspath(__file__))
S = 1024  # master render size

# donut segments roughly matching the 65/35 all-weather mix
SEGMENTS = [
    (0.35, (79, 157, 255)),   # Core World  - blue
    (0.10, (108, 140, 255)),  # Momentum
    (0.10, (124, 114, 255)),  # Quality
    (0.10, (53, 176, 201)),   # EM          - teal
    (0.20, (63, 181, 122)),   # Global Agg  - green
    (0.05, (224, 165, 60)),   # Inflation   - amber
    (0.10, (242, 193, 78)),   # Gold        - gold
]


def gradient(size):
    g = Image.new("RGB", (size, size))
    top, bot = (23, 45, 86), (9, 16, 32)  # navy -> near-black
    px = g.load()
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2 * size)
            px[x, y] = (
                int(top[0] + (bot[0] - top[0]) * t),
                int(top[1] + (bot[1] - top[1]) * t),
                int(top[2] + (bot[2] - top[2]) * t),
            )
    return g.convert("RGBA")


def rounded(img, radius_frac):
    size = img.size[0]
    mask = Image.new("L", (size, size), 0)
    md = ImageDraw.Draw(mask)
    r = int(size * radius_frac)
    if r > 0:
        md.rounded_rectangle([0, 0, size - 1, size - 1], radius=r, fill=255)
    else:
        md.rectangle([0, 0, size - 1, size - 1], fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def donut(size, scale):
    pie = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(pie)
    cx = cy = size / 2
    R = size * 0.34 * scale
    box = [cx - R, cy - R, cx + R, cy + R]
    start = -90.0
    for frac, color in SEGMENTS:
        end = start + frac * 360.0
        d.pieslice(box, start, end, fill=color + (255,))
        start = end
    # punch a centre hole (ring mask) to make it a donut
    hr = R * 0.56
    mask = Image.new("L", (size, size), 0)
    md = ImageDraw.Draw(mask)
    md.ellipse(box, fill=255)
    md.ellipse([cx - hr, cy - hr, cx + hr, cy + hr], fill=0)
    pie.putalpha(ImageChops.multiply(pie.getchannel("A"), mask))
    return pie


def build(size, radius_frac, scale):
    bg = rounded(gradient(S), radius_frac)
    bg.alpha_composite(donut(S, scale))
    return bg.resize((size, size), Image.LANCZOS)


def main():
    for sz in (192, 512, 32):
        build(sz, 0.20, 1.0).save(os.path.join(OUT, f"icon-{sz}.png"))
    build(512, 0.0, 0.82).save(os.path.join(OUT, "icon-maskable-512.png"))
    build(180, 0.0, 0.86).save(os.path.join(OUT, "apple-touch-icon.png"))
    build(512, 0.22, 1.0).save(os.path.join(OUT, "logo.png"))
    print("icons written to", OUT)


if __name__ == "__main__":
    main()
