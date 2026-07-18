# -*- coding: utf-8 -*-
"""Render icon.png from the same shape definitions as icon.svg.

Pillow doesn't read SVG, so we redraw the 12-ray asterisk directly here
at 4x supersampling and downscale with LANCZOS for clean anti-aliased
edges at the final 256x256 size.

Run from this directory:
    python3 _render_icon.py

Output: icon.png (256x256, RGBA, Claude coral on cream).
"""
import math
import os

from PIL import Image, ImageDraw

# Brand palette
CREAM = (240, 238, 230, 255)      # #F0EEE6 — Anthropic "Bone"
CORAL = (204, 120, 92, 255)       # #CC785C — Anthropic accent orange

# Output size + supersampling factor.
OUT_SIZE = 256
SS = 4                             # 4x supersample → render at 1024 then downscale
SIZE = OUT_SIZE * SS
CENTER = SIZE // 2

# Geometry (in 256-space, scaled by SS at render time).
INNER_RADIUS = 16   # how close to center each ray starts
OUTER_RADIUS = 108  # how far each ray reaches
RAY_HALF_WIDTH = 14 # waist width of each ray
RAY_WAIST = 72      # distance from center to where the ray is widest
CORNER_RADIUS = 48  # rounded corner radius of the background square


def ray_polygon(angle_deg):
    """Return a 4-point polygon for one asterisk ray at ``angle_deg``.

    Constructed pointing up (angle 0 = north), then rotated into place.
    Points in order: inner tip, right waist, outer tip, left waist.
    """
    rad = math.radians(angle_deg)
    cos, sin = math.cos(rad), math.sin(rad)

    def rot(x, y):
        # In screen coords y grows downward; rotate around origin then
        # translate to CENTER.
        rx = x * cos - y * sin
        ry = x * sin + y * cos
        return (CENTER + rx * SS, CENTER + ry * SS)

    return [
        rot(0, -INNER_RADIUS),
        rot(RAY_HALF_WIDTH, -RAY_WAIST),
        rot(0, -OUTER_RADIUS),
        rot(-RAY_HALF_WIDTH, -RAY_WAIST),
    ]


def main():
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Rounded-rect background.
    draw.rounded_rectangle(
        [(0, 0), (SIZE - 1, SIZE - 1)],
        radius=CORNER_RADIUS * SS,
        fill=CREAM,
    )

    # 12 rays, every 30°.
    for i in range(12):
        draw.polygon(ray_polygon(i * 30), fill=CORAL)

    # Downscale with LANCZOS for clean anti-aliasing.
    img = img.resize((OUT_SIZE, OUT_SIZE), Image.LANCZOS)

    here = os.path.dirname(os.path.abspath(__file__))
    out_path = os.path.join(here, "icon.png")
    img.save(out_path, "PNG", optimize=True)
    print(f"wrote {out_path} ({OUT_SIZE}x{OUT_SIZE})")


if __name__ == "__main__":
    main()
