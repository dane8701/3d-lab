#!/usr/bin/env python3
"""Render the actual STL parts with a portable CPU depth buffer.

Usage:
    python src/render_preview.py --parts-dir exports/parts --output previews/verre.png

No OpenGL, display server or VTK is required. The image shows the exported
geometry with opaque materials; it does not simulate transparent printed plastic.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont
import trimesh


PARTS = (
    ("corps.stl", "#CDD5DC"),
    ("medaillon-rouge.stl", "#E51020"),
    ("logo-blanc.stl", "#FFFFFF"),
    ("vague-noire.stl", "#151515"),
)
BACKGROUND = (242, 241, 237)
FOREGROUND = (36, 43, 48)
MUTED = (92, 102, 108)


def parse_args() -> argparse.Namespace:
    model_dir = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parts-dir", type=Path, default=model_dir / "exports" / "parts")
    parser.add_argument("--output", type=Path, default=model_dir / "previews" / "verre.png")
    parser.add_argument("--width", type=int, default=1400)
    parser.add_argument("--height", type=int, default=1100)
    parser.add_argument("--no-inset", action="store_true", help="Omit the small top view.")
    args = parser.parse_args()
    if args.width < 400 or args.height < 400:
        parser.error("Image dimensions must be at least 400 pixels.")
    return args


def unit(vector):
    vector = np.asarray(vector, dtype=np.float64)
    return vector / np.linalg.norm(vector)


def read_parts(directory):
    parts = []
    for name, color in PARTS:
        path = directory / name
        if not path.is_file():
            if name == "corps.stl":
                raise SystemExit(f"Required mesh not found: {path}")
            print(f"Optional part absent: {name}", file=sys.stderr)
            continue
        mesh = trimesh.load_mesh(path, process=True)
        if not isinstance(mesh, trimesh.Trimesh) or len(mesh.faces) == 0:
            raise SystemExit(f"Mesh is empty or unreadable: {path}")
        rgb = np.array([int(color[i:i + 2], 16) for i in (1, 3, 5)]) / 255.0
        parts.append((mesh, rgb))
    return parts


def paint_view(canvas, parts, center_px, scale, toward, up_hint, model_center):
    """Orthographically rasterize each actual triangle, keeping the nearest depth."""
    height, width = canvas.shape[:2]
    depth = np.full((height, width), -np.inf, dtype=np.float32)
    x_pixels = np.arange(width, dtype=np.float64) + 0.5
    y_pixels = np.arange(height, dtype=np.float64) + 0.5
    toward = unit(toward)
    right = unit(np.cross(up_hint, toward))
    up = np.cross(toward, right)
    basis = np.stack((right, up, toward), axis=1)
    lights = np.array([unit((-1.3, -1.8, 2.0)),
                       unit((1.8, -0.6, 0.9)),
                       unit((0.0, 1.4, 1.8))])
    half_vector = unit(lights[0] + toward)

    for mesh, color in parts:
        vertices = (np.asarray(mesh.vertices) - model_center) @ basis
        vertices[:, 0] = center_px[0] + scale * vertices[:, 0]
        vertices[:, 1] = center_px[1] - scale * vertices[:, 1]
        normals = np.asarray(mesh.face_normals)
        visible = np.flatnonzero(normals @ toward > 1e-9)
        face_normals = normals[visible]
        diffuse = np.maximum(face_normals @ lights.T, 0.0)
        illumination = 0.40 + diffuse @ np.array((0.45, 0.20, 0.11))
        specular = 0.10 * np.maximum(face_normals @ half_vector, 0.0) ** 38
        colors = np.clip(color[None, :] * illumination[:, None] +
                         specular[:, None], 0.0, 1.0)
        colors = np.round(colors * 255).astype(np.uint8)
        triangles = vertices[np.asarray(mesh.faces)[visible]]

        for triangle, rgb in zip(triangles, colors):
            x0, y0, z0 = triangle[0]
            x1, y1, z1 = triangle[1]
            x2, y2, z2 = triangle[2]
            denominator = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
            if abs(denominator) < 1e-12:
                continue
            xmin = max(0, int(np.ceil(min(x0, x1, x2) - 0.5)))
            xmax = min(width - 1, int(np.floor(max(x0, x1, x2) - 0.5)))
            ymin = max(0, int(np.ceil(min(y0, y1, y2) - 0.5)))
            ymax = min(height - 1, int(np.floor(max(y0, y1, y2) - 0.5)))
            if xmin > xmax or ymin > ymax:
                continue
            xx = x_pixels[xmin:xmax + 1][None, :]
            yy = y_pixels[ymin:ymax + 1][:, None]
            first = ((y1 - y2) * (xx - x2) + (x2 - x1) * (yy - y2)) / denominator
            second = ((y2 - y0) * (xx - x2) + (x0 - x2) * (yy - y2)) / denominator
            third = 1.0 - first - second
            inside = (first >= -1e-8) & (second >= -1e-8) & (third >= -1e-8)
            if not inside.any():
                continue
            surface_z = first * z0 + second * z1 + third * z2
            old_depth = depth[ymin:ymax + 1, xmin:xmax + 1]
            update = inside & (surface_z > old_depth)
            if update.any():
                old_depth[update] = surface_z[update]
                canvas[ymin:ymax + 1, xmin:xmax + 1][update] = rgb


def main() -> None:
    args = parse_args()
    parts = read_parts(args.parts_dir)
    bounds = np.array([mesh.bounds for mesh, _ in parts])
    lower = bounds[:, 0, :].min(axis=0)
    upper = bounds[:, 1, :].max(axis=0)
    size = upper - lower
    center = (lower + upper) / 2
    size_z = float(size[2])
    size_xy = float(max(size[:2]))
    if size_z <= 0 or size_xy <= 0:
        raise SystemExit("The input geometry has degenerate bounds.")

    # Supersampling antialiases narrow letter strokes and rib silhouettes.
    factor = 2
    width, height = args.width * factor, args.height * factor
    canvas = np.empty((height, width, 3), dtype=np.uint8)
    canvas[:] = BACKGROUND
    print("Rendering the exported meshes...", flush=True)
    paint_view(canvas, parts, (0.47 * width, 0.49 * height),
               min(height / (1.28 * size_z), width / (3.1 * size_xy)),
               (0.48, -2.45, 0.88), (0, 0, 1), center)

    if not args.no_inset:
        inset_size = int(min(0.22 * width, 0.28 * height))
        inset = np.empty((inset_size, inset_size, 3), dtype=np.uint8)
        inset[:] = BACKGROUND
        paint_view(inset, parts, (inset_size / 2, inset_size / 2),
                   inset_size / (1.20 * size_xy), (0, 0, 1), (0, 1, 0), center)
        left = round(0.86 * width - inset_size / 2)
        top = round(0.25 * height - inset_size / 2)
        canvas[top:top + inset_size, left:left + inset_size] = inset

    image = Image.fromarray(canvas)
    draw = ImageDraw.Draw(image)
    # Matplotlib ships these fonts, so the script is portable across operating systems.
    from matplotlib import font_manager

    def font(points, bold=False):
        path = font_manager.findfont(font_manager.FontProperties(
            family="DejaVu Sans", weight="bold" if bold else "normal"))
        return ImageFont.truetype(path, max(10 * factor, round(points * height / 1100)))

    def text(label, x, y, points, color=FOREGROUND, bold=False, centered=False):
        face = font(points, bold)
        px, py = round(x * width), round(y * height)
        if centered:
            px -= round(draw.textlength(label, font=face) / 2)
        draw.text((px, py), label, font=face, fill=color)

    text("VERRE CANNELÉ", 0.045, 0.040, 31, bold=True)
    text("Dane SAMA · chill with me", 0.045, 0.085, 19, MUTED)
    text("APERÇU DU MAILLAGE", 0.045, 0.878, 15, MUTED)
    text(f"Hauteur {size_z:.0f} mm  ·  Ø max. {size_xy:.0f} mm", 0.045, 0.912, 20)
    text("Dimensions estimées · rendu opaque", 0.045, 0.949, 16, MUTED)
    if not args.no_inset:
        text("VUE DE DESSUS", 0.86, 0.385, 15, MUTED, centered=True)

    image = image.resize((args.width, args.height), Image.Resampling.LANCZOS)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    image.save(args.output)
    print(f"Preview saved: {args.output} ({args.width} × {args.height})", flush=True)


if __name__ == "__main__":
    main()
