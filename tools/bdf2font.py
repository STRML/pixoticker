#!/usr/bin/env python3
"""Convert an X11 BDF pixel font into a Python glyph table.

    tools/bdf2font.py 4x6.bdf > tools/font46.py

The BDFs in u8g2's tools/font/bdf (Misc-Fixed, public domain) are hinted at
their design size, so they render exactly as drawn with no scaling. Each glyph
becomes one string of 0s and 1s per pixel row, which is easy to eyeball in a diff.
"""
import sys

def header(name, path, w, h, advance, cols):
    return (
        '"""A %d pixel font, generated from %s by tools/bdf2font.py.\n\n'
        "X11 Misc-Fixed, public domain. Every glyph is %d columns and %d rows,\n"
        "so a line advances %d pixels and %d characters fit the 128 px panel.\n"
        '"""\n\nGLYPHS = {\n' % (h, path, w, h, advance, cols)
    )


def footer(w, h, advance):
    return """}

W, H, ADVANCE = %d, %d, %d
MISSING = "?"


def glyph(ch):
    return GLYPHS.get(ch, GLYPHS[MISSING])


def width(text):
    return len(text) * ADVANCE


def draw(d, xy, text, color):
    \"\"\"Blit with no antialiasing: each '1' becomes one pixel.\"\"\"
    x0, y0 = xy
    for i, ch in enumerate(text):
        rows = glyph(ch)
        for row, bits in enumerate(rows):
            for col, bit in enumerate(bits):
                if bit == "1":
                    d.point((x0 + i * ADVANCE + col, y0 + row), fill=color)
    return x0 + len(text) * ADVANCE
""" % (w, h, advance)


def parse(path):
    glyphs = {}
    font_bbx = None
    enc = None
    bbx = None
    rows = None
    in_bitmap = False
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            if line.startswith("FONTBOUNDINGBOX"):
                font_bbx = [int(v) for v in line.split()[1:5]]
            elif line.startswith("STARTCHAR"):
                enc, bbx, rows, in_bitmap = None, None, [], False
            elif line.startswith("ENCODING"):
                enc = int(line.split()[1])
            elif line.startswith("BBX"):
                bbx = [int(v) for v in line.split()[1:5]]
            elif line.startswith("BITMAP"):
                in_bitmap = True
            elif line.startswith("ENDCHAR"):
                if enc is not None and 32 <= enc <= 126 and bbx and rows:
                    glyphs[chr(enc)] = render(rows, bbx)
                elif enc is not None and enc >= 0:
                    glyphs[chr(enc)] = None
            elif in_bitmap:
                rows.append(line)
    return glyphs, font_bbx


def render(rows, bbx):
    w, h, xoff, yoff = bbx
    out = []
    for row in rows:
        val = int(row, 16) if row else 0
        # hex rows are left aligned in whole bytes; the top w bits are the ink
        val >>= max(0, len(row) * 4 - w)
        bits = "".join("1" if val & (1 << (w - 1 - i)) else "0" for i in range(w))
        out.append(bits)
    return out, (w, h, xoff, yoff)


def main():
    path = sys.argv[1]
    name = sys.argv[2] if len(sys.argv) > 2 else path.split("/")[-1].replace(".bdf", "")
    raws, font_bbx = parse(path)
    # one grid for the whole font: place every glyph by its own bbox offset
    h = font_bbx[1]
    w = font_bbx[0]
    advance = font_bbx[0] + (1 if name.startswith("5x") else 0)
    cells = {}
    for ch, item in raws.items():
        if not item:
            continue
        bits, (gw, gh, xoff, yoff) = item
        grid = [["0"] * w for _ in range(h)]
        base = h - 1 + font_bbx[3]  # rows sit on the font baseline
        top = base - (yoff + gh) + 1
        for y, line in enumerate(bits):
            for x, bit in enumerate(line):
                if bit != "1":
                    continue
                gx, gy = xoff + x, top + y
                if 0 <= gx < w and 0 <= gy < h:
                    grid[gy][gx] = "1"
        cells[ch] = "".join(grid[y][x] for y in range(h) for x in range(w))

    out = [header(name, path, w, h, advance, (128 - 2) // advance)]
    for ch in sorted(cells):
        rows = '", "'.join(cells[ch][i * w:(i + 1) * w] for i in range(h))
        out.append(f'    {ch!r}: ("{rows}"),\n')
    out.append(footer(w, h, advance))


    sys.stdout.write("".join(out))


if __name__ == "__main__":
    main()
