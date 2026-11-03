#!/usr/bin/env python3
"""Build the estimated yellow funnel from a single photographic reference.

Run from any directory: python models/entonnoir-jaune/src/generate.py
Dimensions are in millimetres. The outlet lies on Z=0 and remains open.
wall_radial_mm describes a radial offset, not constant normal thickness.
No photograph or external source file is needed to regenerate the model.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import zipfile

import manifold3d as mf
import numpy as np
import trimesh

ROOT = Path(__file__).resolve().parents[1]


def log(message):
    print(message, flush=True)


def as_manifold(mesh):
    mesh = mesh.copy()
    mesh.merge_vertices()
    mesh.fix_normals()
    result = mf.Manifold(mf.Mesh64(np.asarray(mesh.vertices, dtype=np.float64),
                                  np.asarray(mesh.faces, dtype=np.uint64)))
    if result.status() != mf.Error.NoError:
        raise ValueError(f"Invalid solid: {result.status()}")
    return result


def as_mesh(solid):
    raw = solid.to_mesh64()
    mesh = trimesh.Trimesh(vertices=np.asarray(raw.vert_properties)[:, :3],
                           faces=np.asarray(raw.tri_verts), process=True)
    mesh.fix_normals()
    return mesh


def printable_mesh(solid, scale):
    mesh = as_mesh(solid)
    mesh.apply_scale(scale)
    # Validate the precision actually used in binary STL files.
    mesh.vertices = mesh.vertices.astype(np.float32).astype(np.float64)
    mesh.merge_vertices()
    mesh.update_faces(mesh.nondegenerate_faces())
    mesh.update_faces(mesh.unique_faces())
    mesh.remove_unreferenced_vertices()
    mesh.fix_normals()
    if not mesh.is_watertight or not mesh.is_volume:
        raise ValueError("STL quantization did not preserve a closed positive solid")
    return mesh


def smoothstep(value):
    value = np.clip(value, 0.0, 1.0)
    return value * value * (3.0 - 2.0 * value)


def validate_parameters(p):
    positive = ["scale", "mouth_outer_diameter_mm", "body_height_mm",
                "outlet_outer_diameter_mm", "outlet_inner_diameter_mm",
                "nozzle_top_outer_diameter_mm", "nozzle_length_mm",
                "transition_height_mm", "wall_radial_mm", "lip_height_mm",
                "lip_blend_height_mm", "loop_external_width_mm",
                "loop_external_height_mm", "loop_tube_diameter_mm"]
    for name in positive:
        if not math.isfinite(p[name]) or p[name] <= 0:
            raise ValueError(f"{name} must be finite and positive")
    if p["outlet_inner_diameter_mm"] >= p["outlet_outer_diameter_mm"]:
        raise ValueError("The outlet must retain a material wall")
    if p["nozzle_top_outer_diameter_mm"] <= 2 * p["wall_radial_mm"]:
        raise ValueError("The nozzle inlet must remain open")
    if p["nozzle_length_mm"] + p["transition_height_mm"] >= p["body_height_mm"] - p["lip_height_mm"] - p["lip_blend_height_mm"]:
        raise ValueError("The nozzle and transition leave no conical bowl")
    if p["loop_external_width_mm"] <= 2 * p["loop_tube_diameter_mm"]:
        raise ValueError("The loop must retain a clear eyelet")
    n = p["angular_segments"]
    facets = p["cone_facets"]
    if not isinstance(n, int) or not isinstance(facets, int) or facets < 6 or n < 4 * facets or n % facets:
        raise ValueError("angular_segments must be an integer multiple of cone_facets")


def profile_parameters(p):
    z0 = p["nozzle_length_mm"]
    span = p["transition_height_mm"]
    top_z = p["body_height_mm"] - p["lip_height_mm"]
    r0 = p["nozzle_top_outer_diameter_mm"] / 2
    bottom_r = p["outlet_outer_diameter_mm"] / 2
    initial_slope = (r0 - bottom_r) / z0
    # A quadratic transition joins the shallow spout and straight cone C1.
    slope = (p["mouth_outer_diameter_mm"] / 2 - r0 - initial_slope * span / 2) / (top_z - z0 - span / 2)
    return z0, span, top_z, r0, bottom_r, initial_slope, slope


def outer_radius(z, p):
    z0, span, _, r0, bottom_r, initial_slope, slope = profile_parameters(p)
    if z <= z0:
        return bottom_r + initial_slope * z
    t = z - z0
    if t <= span:
        return r0 + initial_slope * t + (slope - initial_slope) * t * t / (2 * span)
    return r0 + (initial_slope + slope) * span / 2 + slope * (t - span)


def make_shell(p):
    n = p["angular_segments"]
    theta = 2 * np.pi * np.arange(n) / n
    z0, span, top_z, _, _, _, _ = profile_parameters(p)
    # A regular polygon radius gives 24 planar cone panels; the round nozzle
    # and lip are blended into it without a discontinuous radial step.
    half_panel = np.pi / p["cone_facets"]
    phase = (theta + half_panel) % (2 * half_panel) - half_panel
    panel_factor = np.cos(half_panel) / np.cos(phase)
    z_values = np.unique(np.concatenate((
        [0.0, z0], np.linspace(z0, z0 + span, 25),
        [z0 + span, top_z - p["lip_blend_height_mm"]],
        np.linspace(top_z - p["lip_blend_height_mm"], top_z, 13))))

    outer, inner = [], []
    for z in z_values:
        factor = smoothstep((z - z0) / span) * smoothstep((top_z - z) / p["lip_blend_height_mm"])
        shape = 1.0 + factor * (panel_factor - 1.0)
        r = outer_radius(float(z), p)
        if z <= z0:
            inner_bottom = p["outlet_inner_diameter_mm"] / 2
            inner_top = p["nozzle_top_outer_diameter_mm"] / 2 - p["wall_radial_mm"]
            inside = inner_bottom + (inner_top - inner_bottom) * z / z0
        else:
            inside = r - p["wall_radial_mm"]
        if inside <= 0:
            raise ValueError("The fluid passage collapsed")
        outer.append((r * shape, float(z)))
        inner.append((inside * shape, float(z)))

    # Elliptical rolled edge, ending at the inner wall; maximum height and
    # mouth diameter remain exactly the configured body dimensions.
    lip_mid_r = p["mouth_outer_diameter_mm"] / 2 - p["wall_radial_mm"] / 2
    lip = []
    for a in np.linspace(0, np.pi, 25)[1:-1]:
        lip.append((np.full(n, lip_mid_r + p["wall_radial_mm"] / 2 * np.cos(a)),
                    top_z + p["lip_height_mm"] * np.sin(a)))
    rings = outer + lip + list(reversed(inner))
    vertices = np.concatenate([np.column_stack((r * np.cos(theta), r * np.sin(theta), np.full(n, z)))
                               for r, z in rings])
    faces = []
    for k in range(len(rings)):
        next_k = (k + 1) % len(rings)
        for i in range(n):
            j = (i + 1) % n
            a, b, c, d = k * n + i, k * n + j, next_k * n + j, next_k * n + i
            faces.extend(((a, b, c), (a, c, d)))
    shell = trimesh.Trimesh(vertices=vertices, faces=faces, process=True)
    shell.fix_normals()
    if not shell.is_volume:
        raise ValueError("The funnel shell is not a closed solid")
    return as_manifold(shell).simplify(p["mesh_tolerance_mm"])


def loop_frame(p):
    angle = np.radians(p["loop_angle_deg"])
    radial = np.array((np.cos(angle), np.sin(angle), 0.0))
    tangent = np.array((-np.sin(angle), np.cos(angle), 0.0))
    tube_r = p["loop_tube_diameter_mm"] / 2
    a = p["loop_external_width_mm"] / 2 - tube_r
    b = p["loop_external_height_mm"] / 2 - tube_r
    radial_center = p["mouth_outer_diameter_mm"] / 2 - p["wall_radial_mm"] / 2
    center_z = p["body_height_mm"] + p["loop_above_rim_mm"] - p["loop_external_height_mm"] / 2
    center = radial * radial_center + np.array((0.0, 0.0, center_z))
    return radial, tangent, tube_r, a, b, center


def make_loop(p):
    radial, tangent, tube_r, a, b, center = loop_frame(p)
    up = np.array((0.0, 0.0, 1.0))
    count = p["loop_segments"]
    sides = p["loop_tube_segments"]
    vertices = []
    for t in np.arange(count) * 2 * np.pi / count:
        c = center + a * np.cos(t) * tangent + b * np.sin(t) * up
        normal = b * np.cos(t) * tangent + a * np.sin(t) * up
        normal /= np.linalg.norm(normal)
        for v in np.arange(sides) * 2 * np.pi / sides:
            vertices.append(c + tube_r * (np.cos(v) * normal + np.sin(v) * radial))
    faces = []
    for i in range(count):
        for j in range(sides):
            a0, b0 = i * sides + j, ((i + 1) % count) * sides + j
            c0, d0 = ((i + 1) % count) * sides + (j + 1) % sides, i * sides + (j + 1) % sides
            faces.extend(((a0, b0, c0), (a0, c0, d0)))
    loop = trimesh.Trimesh(vertices=vertices, faces=faces, process=True)
    loop.fix_normals()
    return as_manifold(loop)


def write_3mf(mesh, destination):
    """One yellow assembled solid, geometry only, without printer presets."""
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<model unit="millimeter" xml:lang="fr-FR" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">',
             '<metadata name="Title">Entonnoir jaune avec anneau</metadata>',
             '<resources><basematerials id="1"><base name="Jaune" displaycolor="#FFC52FFF"/></basematerials>',
             '<object id="2" type="model" name="Entonnoir jaune" pid="1" pindex="0"><mesh><vertices>']
    lines.extend(f'<vertex x="{x:.9g}" y="{y:.9g}" z="{z:.9g}"/>' for x, y, z in mesh.vertices)
    lines.append('</vertices><triangles>')
    lines.extend(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in mesh.faces)
    lines.append('</triangles></mesh></object></resources><build><item objectid="2"/></build></model>')
    with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr('3D/3dmodel.model', '\n'.join(lines))
        archive.writestr('_rels/.rels', '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
        archive.writestr('[Content_Types].xml', '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>')


def mesh_report(mesh):
    return {"vertices": len(mesh.vertices), "triangles": len(mesh.faces),
            "watertight": bool(mesh.is_watertight),
            "winding_consistent": bool(mesh.is_winding_consistent),
            "positive_volume": bool(mesh.is_volume),
            "connected_components": len(mesh.split(only_watertight=False)),
            "volume_mm3": round(float(mesh.volume), 6),
            "bounds_mm": np.round(mesh.bounds, 6).tolist()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--parameters', type=Path, default=ROOT / 'src/parameters.json')
    args = parser.parse_args()
    p = json.loads(args.parameters.read_text(encoding='utf-8'))
    validate_parameters(p)
    exports = ROOT / 'exports'
    exports.mkdir(parents=True, exist_ok=True)
    scale = p['scale']

    log('Building faceted hollow funnel, open tube and rounded lip...')
    shell = make_shell(p)
    loop = make_loop(p)
    overlap = shell ^ loop
    overlap_volume = overlap.volume()
    if overlap_volume <= 1.0:
        raise ValueError('The hanging loop does not overlap the funnel sufficiently')
    log('Joining hanging loop and exporting printable solid...')
    combined = (shell + loop).simplify(p['mesh_tolerance_mm'])
    mesh = printable_mesh(combined, scale)
    destination = exports / 'entonnoir.stl'
    mesh.export(destination)
    write_3mf(mesh, exports / 'entonnoir.3mf')

    saved = trimesh.load_mesh(destination, process=True)
    report = mesh_report(saved)
    if not (report['watertight'] and report['winding_consistent'] and report['positive_volume'] and report['connected_components'] == 1):
        raise ValueError(f'Saved STL failed validation: {report}')
    saved_solid = as_manifold(saved)

    # Probe the entire actual saved model with an extended axial cylinder.
    # A closed manifold alone would not detect an accidentally blocked outlet.
    probe_radius = (p['outlet_inner_diameter_mm'] / 2 - 0.1) * scale
    probe_length = (p['body_height_mm'] + p['loop_above_rim_mm'] + 20) * scale
    axis_probe = mf.Manifold.cylinder(probe_length, probe_radius, circular_segments=128).translate((0, 0, -10 * scale))
    blocked_volume = abs((saved_solid ^ axis_probe).volume())
    if blocked_volume > 1e-7 * scale ** 3:
        raise ValueError(f'The fluid passage is blocked: {blocked_volume} mm3')

    radial, _, _, _, _, center = loop_frame(p)
    eyelet_radius = 1.9 * scale
    eyelet_center = center * scale + np.array((0, 0, 1.0 * scale))
    # Rotate a Z-axis pin into the horizontal radial direction through the eye.
    eyelet_pin = mf.Manifold.cylinder(12 * scale, eyelet_radius, circular_segments=96, center=True)
    eyelet_pin = eyelet_pin.rotate((0, 90, 0)).rotate((0, 0, p['loop_angle_deg'])).translate(eyelet_center)
    eyelet_blocked = abs((saved_solid ^ eyelet_pin).volume())
    if eyelet_blocked > 1e-7 * scale ** 3:
        raise ValueError(f'The eyelet probe is blocked: {eyelet_blocked} mm3')

    scene = trimesh.load(exports / 'entonnoir.3mf', force='scene')
    if len(scene.geometry) != 1 or not all(m.is_watertight and m.is_volume for m in scene.geometry.values()):
        raise ValueError('Saved 3MF failed validation')
    result = {
        'units': 'millimeter', 'estimated_from_one_photo': True,
        'physical_print_tested': False,
        'design_dimensions_mm': {
            'mouth_outer_diameter': p['mouth_outer_diameter_mm'] * scale,
            'body_height': p['body_height_mm'] * scale,
            'overall_height_with_loop': float(saved.bounds[1, 2] - saved.bounds[0, 2]),
            'nozzle_length': p['nozzle_length_mm'] * scale,
            'outlet_outer_diameter': p['outlet_outer_diameter_mm'] * scale,
            'outlet_inner_diameter': p['outlet_inner_diameter_mm'] * scale,
            'wall_radial_offset': p['wall_radial_mm'] * scale,
            'loop_external_width': p['loop_external_width_mm'] * scale,
            'loop_external_height': p['loop_external_height_mm'] * scale,
            'loop_tube_diameter': p['loop_tube_diameter_mm'] * scale},
        'wall_note': 'Radial offset; normal wall thickness varies with cone slope and faceting.',
        'cone_facets': p['cone_facets'], 'stl': report,
        'loop_shell_overlap_volume_mm3': round(overlap_volume * scale ** 3, 6),
        'axis_probe': {'diameter_mm': 2 * probe_radius, 'intersection_volume_mm3': blocked_volume,
                       'continuous_passage_clear': blocked_volume <= 1e-7 * scale ** 3},
        'eyelet_probe': {'diameter_mm': 2 * eyelet_radius, 'intersection_volume_mm3': eyelet_blocked,
                         'clear': eyelet_blocked <= 1e-7 * scale ** 3},
        'three_mf': {'mesh_count': len(scene.geometry), 'bounds_mm': np.round(scene.bounds, 6).tolist()}}
    (exports / 'validation.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    log(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
