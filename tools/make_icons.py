"""
Draws the Getsu app icon and writes getsu.ico and getsu-icon.png to the repository root.

The geometry is the approved rounded-square logo (opt/mockup/logo/getsu-logo.svg for the 5-bar design at
32 px and up, getsu-logo-small.svg for the 3-bar design at 16 to 24 px). The shapes are copied here as
numbers, so the script needs no SVG renderer. Each icon is drawn at 4x and reduced with Lanczos resampling.

Run from the repository root:
    D:\\dev\\project\\getsu\\pyenv\\Scripts\\python.exe tools\\make_icons.py
"""
import io
import os
import struct

from PIL import Image, ImageDraw

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ICO_PATH = os.path.join(REPO_ROOT, "getsu.ico")
PNG_PATH = os.path.join(REPO_ROOT, "getsu-icon.png")

ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
SMALL_DESIGN_MAX = 24  # sizes up to this use the 3-bar design
SUPERSAMPLE = 4

# Vertical gradient of the tile, top to bottom.
GRADIENT_TOP = (0x2F, 0x97, 0xFF)
GRADIENT_BOTTOM = (0x0A, 0x74, 0xE8)

# 5-bar design: 1024 unit grid (getsu-logo.svg). Bars are (x, y, width, height).
LARGE_DESIGN = {
    "view": 1024,
    "tile_radius": 232,
    "bars": ((190, 382, 84, 260), (330, 277, 84, 470), (470, 192, 84, 640),
             (610, 262, 84, 500), (750, 362, 84, 300)),
    "bar_radius": 42,
}

# 3-bar design: 16 unit grid (getsu-logo-small.svg).
SMALL_DESIGN = {
    "view": 16,
    "tile_radius": 3.6,
    "bars": ((4, 5, 2, 6), (7, 3, 2, 10), (10, 4, 2, 8)),
    "bar_radius": 1,
}


def draw_icon(size):
    """Returns an RGBA image of the icon at size x size pixels."""
    design = SMALL_DESIGN if size <= SMALL_DESIGN_MAX else LARGE_DESIGN
    n = size * SUPERSAMPLE
    k = n / design["view"]

    tile_mask = Image.new("L", (n, n), 0)
    ImageDraw.Draw(tile_mask).rounded_rectangle(
        (0, 0, n - 1, n - 1), radius=round(design["tile_radius"] * k), fill=255
    )

    gradient = Image.new("RGBA", (n, n))
    gradient_draw = ImageDraw.Draw(gradient)
    for y in range(n):
        t = y / (n - 1)
        colour = tuple(round(a + (b - a) * t) for a, b in zip(GRADIENT_TOP, GRADIENT_BOTTOM))
        gradient_draw.line((0, y, n, y), fill=colour + (255,))

    canvas = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    canvas.paste(gradient, (0, 0), tile_mask)

    bar_mask = Image.new("L", (n, n), 0)
    bar_draw = ImageDraw.Draw(bar_mask)
    for x, y, w, h in design["bars"]:
        bar_draw.rounded_rectangle(
            (round(x * k), round(y * k), round((x + w) * k) - 1, round((y + h) * k) - 1),
            radius=round(design["bar_radius"] * k),
            fill=255,
        )
    canvas.paste((255, 255, 255, 255), (0, 0, n, n), bar_mask)

    return canvas.resize((size, size), Image.Resampling.LANCZOS)


def _dib_entry(image):
    """BMP (DIB) body for an ICO entry: 32 bpp BGRA rows bottom-up, then an all-opaque AND mask."""
    width, height = image.size
    flipped = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    pixels = flipped.tobytes("raw", "BGRA")
    and_row = ((width + 31) // 32) * 4
    header = struct.pack("<IiiHHIIiiII", 40, width, height * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    return header + pixels + bytes(and_row * height)


def _png_entry(image):
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def write_ico(path, images):
    """Writes a multi-size ICO. Sizes below 256 are BMP entries, 256 is a PNG entry (both are standard in Windows)."""
    bodies = []
    for image in images:
        bodies.append(_png_entry(image) if image.width >= 256 else _dib_entry(image))
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    directory = b""
    for image, body in zip(images, bodies):
        width = 0 if image.width >= 256 else image.width
        height = 0 if image.height >= 256 else image.height
        directory += struct.pack("<BBBBHHII", width, height, 0, 0, 1, 32, len(body), offset)
        offset += len(body)
    with open(path, "wb") as fh:
        fh.write(header + directory + b"".join(bodies))


def main():
    images = {size: draw_icon(size) for size in ICO_SIZES}
    write_ico(ICO_PATH, [images[size] for size in ICO_SIZES])
    images[256].save(PNG_PATH, format="PNG")
    print(f"wrote {ICO_PATH} sizes {list(ICO_SIZES)}")
    print(f"wrote {PNG_PATH} 256x256")


if __name__ == "__main__":
    main()
