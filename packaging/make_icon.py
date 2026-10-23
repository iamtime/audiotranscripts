"""Draw the app icon (app/build/icon.icns): dark rounded square, waveform, red record dot."""
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

S = 1024
OUT = Path(__file__).resolve().parent.parent / "app" / "build" / "icon.icns"


def draw() -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    m = 100                                       # macOS icon grid: 824 px body, soft shadow
    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle((m, m + 14, S - m, S - m + 14), 185, fill=(0, 0, 0, 120))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(18)))

    body = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    grad = Image.new("RGBA", (S, S))
    gd = ImageDraw.Draw(grad)
    for y in range(S):
        t = y / S
        gd.line([(0, y), (S, y)], fill=(int(38 - 18 * t), int(42 - 20 * t), int(52 - 24 * t), 255))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((m, m, S - m, S - m), 185, fill=255)
    body.paste(grad, (0, 0), mask)
    img.alpha_composite(body)

    d = ImageDraw.Draw(img)
    heights = [90, 170, 260, 360, 250, 400, 300, 190, 120]
    bw, gap = 34, 22
    x0 = S / 2 - (len(heights) * bw + (len(heights) - 1) * gap) / 2 - 70
    cy = S / 2 + 10
    for i, h in enumerate(heights):
        x = x0 + i * (bw + gap)
        d.rounded_rectangle((x, cy - h / 2, x + bw, cy + h / 2), bw / 2, fill=(236, 238, 242, 255))

    r = 92
    cx, cyy = S - m - 175, m + 175
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((cx - r - 30, cyy - r - 30, cx + r + 30, cyy + r + 30), fill=(255, 80, 70, 110))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(26)))
    d.ellipse((cx - r, cyy - r, cx + r, cyy + r), fill=(255, 84, 74, 255))
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
