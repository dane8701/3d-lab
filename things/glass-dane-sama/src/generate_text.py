#!/usr/bin/env python3
"""Generate outlined lettering for the curved medallion without a source photo.

Edit lettering.json, then run this file before generate.py. Font outlines come
from the DejaVu fonts bundled with Matplotlib. Width and height are configurable
independently to give the main lettering a bold, condensed italic appearance.
The original decorative black wave is retained verbatim in lettering.json.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from xml.sax.saxutils import escape

import matplotlib
from matplotlib.font_manager import FontProperties
from matplotlib.textpath import TextPath
from shapely import affinity, make_valid
from shapely.geometry import Point, Polygon
from shapely.geometry.polygon import orient
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]


def polygons(geometry):
    """Yield only polygon members, including those in GeometryCollections."""
    if isinstance(geometry, Polygon):
        yield geometry
    elif hasattr(geometry, "geoms"):
        for part in geometry.geoms:
            yield from polygons(part)


def signed_area(points):
    return sum(a[0] * b[1] - b[0] * a[1]
               for a, b in zip(points, points[1:])) / 2.0


def text_geometry(text, font_file):
    """Convert contours to polygons, preserving counters such as a, D and A.

    FreeType gives outer and inner contours opposite winding directions.
    Assign each counter to the smallest containing outer contour before
    merging glyphs, rather than filling every contour as a solid polygon.
    """
    if not text or not text.strip():
        raise ValueError("Each lettering line must contain visible characters")
    font_path = Path(matplotlib.get_data_path()) / "fonts" / "ttf" / font_file
    if Path(font_file).name != font_file or not font_path.is_file():
        raise ValueError(f"Unknown Matplotlib bundled font: {font_file}")
    path = TextPath((0, 0), text, size=100,
                    prop=FontProperties(fname=str(font_path)))
    rings = [(ring, signed_area(ring)) for ring in path.to_polygons()
             if len(ring) >= 4]
    if not rings:
        raise ValueError(f"Text has no outlines: {text!r}")
    outer_sign = 1 if max(rings, key=lambda entry: abs(entry[1]))[1] > 0 else -1
    outer_rings = [ring for ring, area in rings if area * outer_sign > 0]
    inner_rings = [ring for ring, area in rings if area * outer_sign < 0]
    shells = [Polygon(ring) for ring in outer_rings]
    holes = [[] for _ in shells]
    for ring in inner_rings:
        counter = Polygon(ring)
        owners = [index for index, shell in enumerate(shells)
                  if shell.covers(counter.representative_point())
                  and shell.area > counter.area]
        if not owners:
            raise ValueError(f"Unassigned glyph counter in {text!r}")
        owner = min(owners, key=lambda index: shells[index].area)
        holes[owner].append(ring)
    geometry = unary_union([make_valid(Polygon(shell, inner))
                            for shell, inner in zip(outer_rings, holes)])
    if geometry.is_empty or not geometry.is_valid:
        raise ValueError(f"Invalid lettering geometry: {text!r}")
    return geometry


def make_line(line, radius):
    geometry = text_geometry(line["text"], line["font_file"])
    min_x, min_y, max_x, max_y = geometry.bounds
    geometry = affinity.translate(geometry, -(min_x + max_x) / 2,
                                  -(min_y + max_y) / 2)
    width, height = float(line["width_mm"]), float(line["height_mm"])
    if width <= 0 or height <= 0:
        raise ValueError("Lettering width and height must be positive")
    geometry = affinity.scale(geometry, width / (max_x - min_x),
                              height / (max_y - min_y), origin=(0, 0))
    geometry = affinity.translate(geometry, float(line["center_x_mm"]),
                                  float(line["center_y_mm"]))
    # Keep the 0.05 mm expansion in generate.py clear of the disc perimeter.
    if not Point(0, 0).buffer(radius - 0.30, quad_segs=128).covers(geometry):
        raise ValueError(f"Lettering exceeds the medallion: {line['text']!r}")
    info = {"text": line["text"], "font_file": line["font_file"],
            "bounds_mm_at_reference_radius": [round(v, 6) for v in geometry.bounds],
            "polygons": len(list(polygons(geometry))),
            "holes": sum(len(poly.interiors) for poly in polygons(geometry))}
    normalized = affinity.scale(geometry, 1 / radius, 1 / radius, origin=(0, 0))
    return normalized, info


def to_records(geometry):
    records = []
    for polygon in polygons(geometry):
        polygon = orient(polygon, sign=1.0)
        coords = lambda ring: [[round(x, 9), round(y, 9)]
                               for x, y in list(ring.coords)[:-1]]
        records.append({"outer": coords(polygon.exterior),
                        "holes": [coords(ring) for ring in polygon.interiors]})
    return records


def svg_path(record):
    pieces = []
    for ring in [record["outer"], *record.get("holes", [])]:
        if not ring:
            continue
        pieces.append("M" + " L".join(f"{x:.9g},{y:.9g}" for x, y in ring) + " Z")
    return " ".join(pieces)


def write_preview(data, destination):
    title = " / ".join(data["metadata"]["texts"])
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<svg xmlns="http://www.w3.org/2000/svg" viewBox="-1.06 -1.06 2.12 2.12" width="848" height="848" role="img">',
             f'<title>{escape(title)}</title>',
             '<desc>Contours réels du lettrage et du médaillon, sans police externe.</desc>',
             '<g transform="scale(1,-1)">',
             '<circle cx="0" cy="0" r="1" fill="#e51020"/>']
    for key, colour in [("black", "#151515"), ("white", "#ffffff")]:
        for record in data[key]:
            lines.append(f'<path d="{svg_path(record)}" fill="{colour}" fill-rule="evenodd"/>')
    lines.append('</g></svg>')
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "src/lettering.json")
    parser.add_argument("--output", type=Path, default=ROOT / "src/logo-contours.json")
    parser.add_argument("--preview", type=Path, default=ROOT / "previews/lettrage.svg")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    radius = float(config["reference_badge_radius_mm"])
    if radius <= 0:
        raise ValueError("Reference badge radius must be positive")
    shapes, typography = [], []
    for line in config["lines"]:
        geometry, info = make_line(line, radius)
        shapes.append(geometry)
        typography.append(info)
    for index, geometry in enumerate(shapes):
        if any(geometry.intersects(other) for other in shapes[index + 1:]):
            raise ValueError("Lettering lines overlap; adjust lettering.json")
    result = {"white": to_records(unary_union(shapes)),
              "black": config["black_wave"],
              "metadata": {
                  "texts": [line["text"] for line in config["lines"]],
                  "coordinates": "x right, z up; normalized by badge radius",
                  "reference_badge_radius_mm": radius,
                  "outer_winding": "counterclockwise; holes clockwise; closing point omitted",
                  "generation": "generate_text.py; editable lettering.json; bundled Matplotlib DejaVu outlines",
                  "black_wave": "Preserved from the original decorative contour",
                  "typography": typography}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    write_preview(result, args.preview)
    print(json.dumps({"output": str(args.output), "preview": str(args.preview),
                      "texts": result["metadata"]["texts"],
                      "typography": typography}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
