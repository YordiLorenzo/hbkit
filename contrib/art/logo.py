"""Write docs/images/logo.svg and logo-light.svg: the icon beside "hbkit" set in Outfit
SemiBold, with the letters as outlines so the logo needs no font wherever it is shown.

    pip install fonttools brotli
    python contrib/art/logo.py
"""
import pathlib
import re

from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORD, WEIGHT = "hbkit", 600
ICON = 64                      # the icon's own viewBox
CAP = 40                       # cap height of the wordmark beside a 64px icon
GAP = 18

font = instantiateVariableFont(TTFont(ROOT / "site/fonts/outfit.woff2"), {"wght": WEIGHT})
glyphs, cmap, hmtx = font.getGlyphSet(), font.getBestCmap(), font["hmtx"]
ascender = font["hhea"].ascent
scale = CAP / (font["OS/2"].sCapHeight or ascender * .7)

pen = SVGPathPen(glyphs)
x = 0
for ch in WORD:
    name = cmap[ord(ch)]
    # font units are y-up; flip into SVG space with the baseline at y = ascender
    glyphs[name].draw(TransformPen(pen, (1, 0, 0, -1, x, ascender)))
    x += hmtx[name][0]
path = pen.getCommands()

icon = (ROOT / "site/images/icon.svg").read_text()
inner = re.search(r"<svg[^>]*>(.*)</svg>", icon, re.S).group(1)
inner = re.sub(r"<title>.*?</title>", "", inner)

word_w = x * scale
width = ICON + GAP + word_w
# centre the cap height on the icon
top = (ICON - CAP) / 2 - (ascender - (font["OS/2"].sCapHeight or ascender * .7)) * scale

for name, fill in (("logo.svg", "#ecf3f1"), ("logo-light.svg", "#0b171c")):
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:.1f} {ICON}" '
           f'width="{width:.0f}" height="{ICON}"><title>hbkit</title>{inner}'
           f'<path fill="{fill}" transform="translate({ICON + GAP} {top:.2f}) scale({scale:.5f})" '
           f'd="{path}"/></svg>\n')
    (ROOT / "docs/images" / name).write_text(svg)
    print(name, f"{width:.0f}x{ICON}")
