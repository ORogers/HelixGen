"""Draw the HelixGen app icon and render it to PNG.

The mark is authored here rather than painted in an editor, so it stays crisp
at every size macOS asks for - 1024 down to the 16px the Finder list view uses
- and can be adjusted later by changing a number instead of redrawing.

    python3 packaging/icon/make_icon.py --concept pick-helix --out icon.png

Colours are the app's own: the workspace background, and the accent the
Generate button and the chain arrows already use.
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

BACKDROP = "#17181c"
BACKDROP_LIFT = "#23242b"
ACCENT = "#7c5cff"
ACCENT_LIGHT = "#a68dff"
ACCENT_DEEP = "#4b3aa8"

#: macOS rounds its icons at about 22.4% of the tile and insets the tile inside
#: the canvas so a shadow has room.
CANVAS = 1024
TILE = 824
RADIUS = 185
OFFSET = (CANVAS - TILE) / 2


def _backdrop() -> str:
    return f"""
  <defs>
    <linearGradient id="tile" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="{BACKDROP_LIFT}"/>
      <stop offset="1" stop-color="{BACKDROP}"/>
    </linearGradient>
    <linearGradient id="stroke" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="{ACCENT_LIGHT}"/>
      <stop offset="1" stop-color="{ACCENT}"/>
    </linearGradient>
  </defs>
  <rect x="{OFFSET}" y="{OFFSET}" width="{TILE}" height="{TILE}" rx="{RADIUS}"
        fill="url(#tile)"/>
  <rect x="{OFFSET + 2}" y="{OFFSET + 2}" width="{TILE - 4}" height="{TILE - 4}"
        rx="{RADIUS - 2}" fill="none" stroke="#ffffff" stroke-opacity="0.06"
        stroke-width="4"/>"""


def _weave(
    left: float,
    right: float,
    mid: float,
    amp: float,
    crossings: int,
    width: float,
    front: str,
    back: str,
) -> str:
    """Two strands that genuinely pass over and under each other.

    Drawing one strand on top of the other gives a pair of overlapping waves,
    not a helix. What reads as woven is the front strand *changing* at every
    crossing, so each strand is sampled as a polyline, split where they meet,
    and the halves painted in alternating order.
    """
    steps = 60
    period = (right - left) / (crossings / 2)

    def y(x: float, phase: int) -> float:
        return mid + phase * amp * math.cos(2 * math.pi * (x - left) / period)

    def segment(x0: float, x1: float, phase: int) -> str:
        pts = [
            (x0 + (x1 - x0) * i / steps, y(x0 + (x1 - x0) * i / steps, phase))
            for i in range(steps + 1)
        ]
        return f"M {pts[0][0]:.1f} {pts[0][1]:.1f} " + " ".join(
            f"L {px:.1f} {py:.1f}" for px, py in pts[1:]
        )

    # Crossings sit a quarter period in, then every half period after.
    edges = [left, *(left + period * (0.25 + 0.5 * i) for i in range(crossings)), right]
    parts = []
    for index in range(len(edges) - 1):
        x0, x1 = edges[index], edges[index + 1]
        # Which strand is in front alternates from one span to the next.
        for depth, phase in enumerate((1, -1) if index % 2 == 0 else (-1, 1)):
            colour = back if depth == 0 else front
            parts.append(
                f'<path d="{segment(x0, x1, phase)}" fill="none" stroke="{colour}" '
                f'stroke-width="{width}" stroke-linecap="round"/>'
            )
    return "\n  ".join(parts)


def pick_helix() -> str:
    """A plectrum with a helix woven through it.

    The pick is what makes it legible at 16px - a silhouette nothing else in a
    Dock shares - and the helix is the name. Either alone is weaker: a bare
    helix reads as generic DNA, a bare pick as any guitar app.
    """
    body = (
        "M 512 236 "
        "C 648 236, 756 322, 756 436 "
        "C 756 566, 626 734, 512 792 "
        "C 398 734, 268 566, 268 436 "
        "C 268 322, 376 236, 512 236 Z"
    )
    # Centred on the pick's own centre of mass rather than the tile's, so the
    # mark does not float above a dead area.
    return (
        f'<path d="{body}" fill="url(#stroke)"/>\n  '
        + _weave(346, 678, 456, 94, 3, 46, BACKDROP, ACCENT_DEEP)
    )


def helix() -> str:
    """Two strands crossing, with a node where they meet.

    The open form: a double helix and a pair of waveforms at once, with the
    rungs between the strands kept faint so they read as structure rather than
    as part of the mark.
    """
    left, right = 250, 774
    mid = CANVAS / 2
    amp = 150
    width = 54
    span = (right - left) / 2
    c = span * 0.36

    def strand(phase: int) -> str:
        y1, y2 = mid - amp * phase, mid + amp * phase
        return (
            f"M {left} {y1} "
            f"C {left + c} {y1}, {left + span - c} {y2}, {left + span} {y2} "
            f"C {left + span + c} {y2}, {right - c} {y1}, {right} {y1}"
        )

    rungs = "".join(
        f'<line x1="{x}" y1="{mid - r}" x2="{x}" y2="{mid + r}" '
        f'stroke="{ACCENT}" stroke-opacity="0.45" stroke-width="20" '
        f'stroke-linecap="round"/>'
        for x, r in ((381, 112), (643, 112))
    )
    return f"""{rungs}
  <path d="{strand(1)}" fill="none" stroke="url(#stroke)" stroke-width="{width}"
        stroke-linecap="round"/>
  <path d="{strand(-1)}" fill="none" stroke="url(#stroke)" stroke-width="{width}"
        stroke-linecap="round" stroke-opacity="0.75"/>
  <circle cx="512" cy="{mid}" r="34" fill="{ACCENT_LIGHT}"/>"""


def helix_woven() -> str:
    """The same idea drawn as a true weave, strands passing over and under."""
    return _weave(268, 756, 512, 168, 2, 92, "url(#stroke)", ACCENT_DEEP)


def pick() -> str:
    """A plectrum with a level meter through it."""
    body = (
        "M 512 250 "
        "C 640 250, 742 330, 742 438 "
        "C 742 560, 620 720, 512 774 "
        "C 404 720, 282 560, 282 438 "
        "C 282 330, 384 250, 512 250 Z"
    )
    bars = "".join(
        f'<rect x="{512 + dx - 19}" y="{470 - h / 2}" width="38" height="{h}" '
        f'rx="19" fill="{BACKDROP}"/>'
        for dx, h in ((-150, 70), (-90, 150), (-30, 240), (30, 170), (90, 100), (150, 56))
    )
    return f'<path d="{body}" fill="url(#stroke)"/>\n  {bars}'


CONCEPTS = {
    "helix": helix,
    "helix-woven": helix_woven,
    "pick-helix": pick_helix,
    "pick": pick,
}


def build(concept: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{CANVAS}" height="{CANVAS}" '
        f'viewBox="0 0 {CANVAS} {CANVAS}">{_backdrop()}\n  {CONCEPTS[concept]()}\n</svg>'
    )


def render(svg: str, out: Path, size: int) -> None:
    """Rasterise with Qt, which the desktop app already depends on."""
    from PySide6.QtCore import QByteArray, Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer
    from PySide6.QtWidgets import QApplication

    if QApplication.instance() is None:
        QApplication([])
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    QSvgRenderer(QByteArray(svg.encode())).render(painter)
    painter.end()
    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(str(out))


#: What an .icns has to contain. macOS picks per context: 16 is the Finder list
#: view, 32 the desktop, 128 and up the Dock and Get Info.
ICNS_SIZES = (16, 32, 64, 128, 256, 512, 1024)


def write_icns(svg: str, out: Path) -> int:
    """Render every size macOS asks for and pack them with iconutil."""
    import shutil
    import subprocess
    import tempfile

    if shutil.which("iconutil") is None:
        print("iconutil not found; .icns can only be built on macOS", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as work:
        iconset = Path(work) / "HelixGen.iconset"
        iconset.mkdir()
        for size in ICNS_SIZES:
            # Every size is drawn from the vector rather than downsampled from
            # one big raster, so the small ones stay sharp.
            render(svg, iconset / f"icon_{size}x{size}.png", size)
            if size * 2 in ICNS_SIZES or size >= 512:
                render(svg, iconset / f"icon_{size}x{size}@2x.png", size * 2)
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["iconutil", "-c", "icns", str(iconset), "-o", str(out)], check=True
        )
    print(f"{out} ({out.stat().st_size // 1024} KB)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--concept", choices=sorted(CONCEPTS), default="helix")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--size", type=int, default=CANVAS)
    parser.add_argument("--svg", type=Path, help="Also write the SVG source here")
    parser.add_argument(
        "--icns", action="store_true", help="Write a macOS .icns instead of a PNG"
    )
    args = parser.parse_args()

    svg = build(args.concept)
    if args.svg:
        args.svg.parent.mkdir(parents=True, exist_ok=True)
        args.svg.write_text(svg, encoding="utf-8")
    if args.icns:
        return write_icns(svg, args.out)
    render(svg, args.out, args.size)
    print(f"{args.concept} -> {args.out} ({args.size}px)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
