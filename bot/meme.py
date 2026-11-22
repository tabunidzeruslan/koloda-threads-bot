"""Meme layouts with meme fonts (Pillow). Styles:
classic: white Oswald Bold caps with black outline on the image (Impact-like, the old-school meme look)
caption_above: modern Twitter/Threads meme, white bar above the picture with black Roboto text
two_panel_vertical / two_panel_horizontal: two pictures with captions (Drake, До/Після, Очікування/Реальність)."""
from __future__ import annotations

import io
from pathlib import Path

FONTS = Path(__file__).resolve().parent.parent / "data" / "fonts"
SIZE = 1024


def available() -> bool:
    try:
        import PIL  # noqa: F401
        return True
    except ImportError:
        return False


def _font(name: str, size: int, weight: bytes = b"Bold"):
    from PIL import ImageFont
    f = ImageFont.truetype(str(FONTS / name), size)
    try:
        f.set_variation_by_name(weight)
    except Exception:
        pass
    return f


def _wrap(draw, text: str, font, width: int) -> list[str]:
    lines = []
    for para in text.split("\n"):
        cur = ""
        for w in para.split():
            t = (cur + " " + w).strip()
            if draw.textlength(t, font=font) <= width or not cur:
                cur = t
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
    return lines


def _fit(draw, text: str, name: str, width: int, max_h: int, start: int, weight=b"Bold"):
    size = start
    while size > 18:
        f = _font(name, size, weight)
        lines = _wrap(draw, text, f, width)
        h = len(lines) * int(size * 1.18)
        if h <= max_h:
            return f, lines, size
        size -= 4
    f = _font(name, size, weight)
    return f, _wrap(draw, text, f, width), size


def _load(b: bytes, size: tuple[int, int]):
    from PIL import Image, ImageOps
    return ImageOps.fit(Image.open(io.BytesIO(b)).convert("RGB"), size)


def _outlined(draw, xy_center_top, lines, font, size):
    x, y = xy_center_top
    stroke = max(2, size // 12)
    for ln in lines:
        w = draw.textlength(ln, font=font)
        draw.text((x - w / 2, y), ln, font=font, fill="white", stroke_width=stroke, stroke_fill="black")
        y += int(size * 1.18)


def classic(img: bytes, top: str, bottom: str = "") -> bytes:
    from PIL import ImageDraw
    im = _load(img, (SIZE, SIZE))
    d = ImageDraw.Draw(im)
    if top:
        f, lines, s = _fit(d, top.upper(), "Oswald.ttf", SIZE - 60, 260, 96)
        _outlined(d, (SIZE / 2, 20), lines, f, s)
    if bottom:
        f, lines, s = _fit(d, bottom.upper(), "Oswald.ttf", SIZE - 60, 260, 96)
        _outlined(d, (SIZE / 2, SIZE - 30 - len(lines) * int(s * 1.18)), lines, f, s)
    return _png(im)


def caption_above(img: bytes, caption: str) -> bytes:
    from PIL import Image, ImageDraw
    probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    f, lines, s = _fit(probe, caption, "Roboto.ttf", SIZE - 80, 330, 54, b"Medium")
    bar = len(lines) * int(s * 1.25) + 60
    im = Image.new("RGB", (SIZE, SIZE + bar), "white")
    im.paste(_load(img, (SIZE, SIZE)), (0, bar))
    d = ImageDraw.Draw(im)
    y = 30
    for ln in lines:
        d.text((40, y), ln, font=f, fill="black")
        y += int(s * 1.25)
    return _png(im)


def two_panel(imgs: list[bytes], captions: list[str], vertical: bool) -> bytes:
    """vertical: Drake style (pictures left, captions right, two rows). horizontal: side by side, captions on top."""
    from PIL import Image, ImageDraw
    if vertical:
        half = SIZE // 2
        im = Image.new("RGB", (SIZE, SIZE), "white")
        d = ImageDraw.Draw(im)
        for i in range(2):
            im.paste(_load(imgs[min(i, len(imgs) - 1)], (half, half)), (0, i * half))
            f, lines, s = _fit(d, captions[i] if i < len(captions) else "", "Roboto.ttf", half - 60, half - 60, 52, b"Bold")
            y = i * half + (half - len(lines) * int(s * 1.2)) // 2
            for ln in lines:
                d.text((half + 30, y), ln, font=f, fill="black")
                y += int(s * 1.2)
        d.line([(0, half), (SIZE, half)], fill="black", width=4)
        d.line([(half, 0), (half, SIZE)], fill="black", width=4)
        return _png(im)
    half = SIZE // 2
    probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    fits = [_fit(probe, (captions[i] if i < len(captions) else "").upper(), "Oswald.ttf", half - 40, 200, 64) for i in range(2)]
    bar = max(len(l) * int(s * 1.18) for _, l, s in fits) + 40
    im = Image.new("RGB", (SIZE, half + bar), "white")
    d = ImageDraw.Draw(im)
    for i in range(2):
        im.paste(_load(imgs[min(i, len(imgs) - 1)], (half, half)), (i * half, bar))
        f, lines, s = fits[i]
        y = 20
        for ln in lines:
            w = d.textlength(ln, font=f)
            d.text((i * half + (half - w) / 2, y), ln, font=f, fill="black")
            y += int(s * 1.18)
    d.line([(half, 0), (half, half + bar)], fill="black", width=4)
    return _png(im)


def compose(layout: str, imgs: list[bytes], captions: list[str]) -> bytes:
    if layout == "two_panel_vertical":
        return two_panel(imgs, captions, vertical=True)
    if layout == "two_panel_horizontal":
        return two_panel(imgs, captions, vertical=False)
    if layout == "classic":
        return classic(imgs[0], captions[0] if captions else "", captions[1] if len(captions) > 1 else "")
    return caption_above(imgs[0], "\n".join(c for c in captions if c))


def _png(im) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return buf.getvalue()
