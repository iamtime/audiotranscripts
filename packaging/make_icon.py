"""Make the app icon (app/build/icon.icns + icon.png) from the ushi artwork:
the bull's head (packaging/brand/ushi-icon-source.png) on its cream rounded square."""
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

S = 1024
HERE = Path(__file__).resolve().parent
SOURCE = HERE / "brand" / "ushi-icon-source.png"
OUT = HERE.parent / "app" / "build" / "icon.icns"


def draw() -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    m = 100                                       # macOS icon grid: 824 px body, soft shadow
    body = S - 2 * m
    radius = 185
    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle((m, m + 14, S - m, S - m + 14), radius, fill=(36, 37, 47, 90))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(18)))

    # The artwork's own cream background fills the square (as in the design's cream icon)
    art = Image.open(SOURCE).convert("RGBA")
    k = art.width / 1080
    # a stray grey speck under the bull's chin, bottom right
    ImageDraw.Draw(art).rectangle((760 * k, 840 * k, 860 * k, 885 * k), fill=(252, 250, 247, 255))
    art = art.resize((body, body), Image.LANCZOS)
    mask = Image.new("L", (S * 2, S * 2), 0)        # drawn at 2x for a smooth edge
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, body * 2 - 1, body * 2 - 1), radius * 2, fill=255)
    mask = mask.crop((0, 0, body * 2, body * 2)).resize((body, body), Image.LANCZOS)
    img.paste(art, (m, m), mask)
    return img


def main():
    img = draw()
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "icon.iconset"
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                px = size * scale
                name = f"icon_{size}x{size}{'@2x' if scale == 2 else ''}.png"
                img.resize((px, px), Image.LANCZOS).save(iconset / name)
        OUT.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(OUT)], check=True)
        img.save(OUT.with_suffix(".png"))
    print(OUT)


if __name__ == "__main__":
    sys.exit(main())
