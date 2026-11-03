"""Build the EP-0172 open-top tray, fit coupon, and checked STL files.

Dimensions are millimetres. Mounting spacing is measured by the owner.
PCB outline is from the manufacturer; nominal thickness should be checked.
Requires numpy, manifold3d, trimesh, and matplotlib.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import manifold3d as m
import numpy as np
import trimesh

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output" / "housing"

DEFAULTS = {
    "pcb_length": 144.0,
    "pcb_width": 78.0,
    "pcb_thickness": 1.6,
    "hole_spacing_x": 138.0,
    "hole_spacing_y": 72.0,
    "side_clearance": 0.9,
    "under_pcb_clearance": 50.8,
    "wall": 2.4,
    "floor": 3.0,
    "inner_corner_radius": 1.0,
    "boss_leg": 12.0,
    "pilot_diameter": 2.2,
    "pilot_depth": 8.0,
    "mouth_chamfer": 0.4,
}


def rounded_rectangle(x, y, length, width, radius):
    points = []
    for cx, cy, start in (
        (x + length - radius, y + radius, -90),
        (x + length - radius, y + width - radius, 0),
        (x + radius, y + width - radius, 90),
        (x + radius, y + radius, 180),
    ):
        for angle in np.linspace(start, start + 90, 25):
            a = math.radians(angle)
            points.append((cx + radius * math.cos(a), cy + radius * math.sin(a)))
    return m.CrossSection([points])


def hole_centers(p):
    dx = (p["pcb_length"] - p["hole_spacing_x"]) / 2
    dy = (p["pcb_width"] - p["hole_spacing_y"]) / 2
    return [(x, y) for x in (dx, dx + p["hole_spacing_x"])
            for y in (dy, dy + p["hole_spacing_y"])]


def build(p, clearance):
    c, w, f = p["side_clearance"], p["wall"], p["floor"]
    r = p["inner_corner_radius"]
    seat = f + clearance
    height = seat + p["pcb_thickness"]
    inner_l = p["pcb_length"] + 2*c
    inner_w = p["pcb_width"] + 2*c
    outer = rounded_rectangle(-c-w, -c-w, inner_l+2*w, inner_w+2*w, r+w)
    inner = rounded_rectangle(-c, -c, inner_l, inner_w, r)
    shell = m.Manifold.extrude(outer, height) - m.Manifold.extrude(
        inner, height).translate((0, 0, f))
    # Full-height triangular corner bosses overlap both walls and the floor.
    # Each leg runs 12 mm along the inside wall; the hidden overlap fuses it.
    overlap = w/2
    leg = p["boss_leg"]
    for x, sx in ((-c, 1), (p["pcb_length"]+c, -1)):
        for y, sy in ((-c, 1), (p["pcb_width"]+c, -1)):
            points = [(x+sx*dx, y+sy*dy) for dx, dy in
                      ((-overlap, -overlap), (leg+overlap, -overlap),
                       (-overlap, leg+overlap))]
            if sx*sy < 0:
                points.reverse()
            shell += m.Manifold.extrude(m.CrossSection([points]), seat)
    for x, y in hole_centers(p):
        # Keep a solid floor, including on the shallow fit-check version.
        depth = min(p["pilot_depth"], clearance)
        bore = m.Manifold.cylinder(depth + 0.02, p["pilot_diameter"]/2,
                                  circular_segments=64).translate((x, y, seat-depth))
        ch = p["mouth_chamfer"]
        mouth = m.Manifold.cylinder(ch+0.02, p["pilot_diameter"]/2,
                                   p["pilot_diameter"]/2+ch+0.02,
                                   circular_segments=64).translate((x, y, seat-ch))
        shell = shell - bore - mouth
    assert str(shell.status()) == "Error.NoError", shell.status()
    return shell


def to_trimesh(solid):
    data = solid.to_mesh()
    return trimesh.Trimesh(np.asarray(data.vert_properties)[:, :3],
                           np.asarray(data.tri_verts), process=True)


def connected_body_count(mesh):
    parent = list(range(len(mesh.vertices)))

    def find(v):
        while parent[v] != v:
            parent[v] = parent[parent[v]]
            v = parent[v]
        return v

    for face in mesh.faces:
        a, b, c = (int(v) for v in face)
        parent[find(b)] = find(a)
        parent[find(c)] = find(a)
    return len({find(i) for i in range(len(parent))})


def validate(solid, path, p, clearance):
    mesh = to_trimesh(solid)
    mesh.export(path)
    checked = trimesh.load_mesh(path, process=True)
    expected = np.array([
        p["pcb_length"] + 2*(p["side_clearance"]+p["wall"]),
        p["pcb_width"] + 2*(p["side_clearance"]+p["wall"]),
        p["floor"] + clearance + p["pcb_thickness"],
    ])
    assert checked.is_watertight, f"Open edges in {path}"
    assert checked.is_winding_consistent, f"Inconsistent winding in {path}"
    assert checked.is_volume and checked.volume > 0
    bodies = connected_body_count(checked)
    assert bodies == 1, f"Disconnected body in {path}"
    assert np.allclose(checked.extents, expected, atol=0.002)

    # Verify the actual model has the requested empty central working cavity.
    z_floor = p["floor"]
    cavity = m.Manifold.cube((p["pcb_length"]-16, p["pcb_width"]-16,
                             clearance-0.002)).translate((8, 8, z_floor+0.001))
    assert (solid ^ cavity).volume() < 1e-6

    # Check PCB insertion volume and corner support planes independently.
    pcb = m.Manifold.cube((p["pcb_length"], p["pcb_width"],
                          p["pcb_thickness"]-0.002)).translate((0, 0, z_floor+clearance+0.001))
    assert (solid ^ pcb).volume() < 1e-6, "Board collides with housing"
    for x, y in hole_centers(p):
        seat_sample = m.Manifold.cylinder(0.02, 2.0, circular_segments=32).translate(
            (x, y, z_floor+clearance-0.02))
        above_sample = seat_sample.translate((0, 0, 0.04))
        assert (solid ^ seat_sample).volume() > 0.1
        assert (solid ^ above_sample).volume() < 1e-6

    return mesh, {
        "file": path.name,
        "watertight": bool(checked.is_watertight),
        "consistent_normals": bool(checked.is_winding_consistent),
        "connected_bodies": bodies,
        "triangles": len(checked.faces),
        "outer_dimensions_mm": checked.extents.tolist(),
        "solid_volume_cm3": float(checked.volume/1000),
        "inside_floor_z_mm": z_floor,
        "pcb_underside_z_mm": z_floor+clearance,
        "floor_to_pcb_underside_mm": clearance,
        "pcb_insertion_collision_mm3": 0,
    }


def write_scad(p, output_dir=OUT):
    lines = [
        "// GeeekPi EP-0172 Plus: all dimensions in mm.",
        "// Hole spacing 138 x 72 measured by owner. Check physical fit before full print.",
        "// part: housing or fit_check. Export STL with millimetre units.",
        'part = "housing";',
        "$fn = 96;",
    ]
    lines += [f"{k} = {v};" for k, v in p.items()]
    lines += [r'''
clearance = part == "fit_check" ? 10 : under_pcb_clearance;
base_floor = part == "fit_check" ? 0 : floor;
seat = base_floor + clearance;
height = seat + pcb_thickness;
in_l = pcb_length + 2*side_clearance;
in_w = pcb_width + 2*side_clearance;
inset_x = (pcb_length-hole_spacing_x)/2;
inset_y = (pcb_width-hole_spacing_y)/2;

module round_rect(l,w,r) {
    hull() for (x=[r,l-r], y=[r,w-r]) translate([x,y]) circle(r=r);
}
module housing() {
    difference() {
        union() {
            difference() {
                translate([-side_clearance-wall,-side_clearance-wall,0])
                    linear_extrude(height=height)
                    round_rect(in_l+2*wall,in_w+2*wall,inner_corner_radius+wall);
                translate([-side_clearance,-side_clearance,base_floor])
                    linear_extrude(height=height)
                    round_rect(in_l,in_w,inner_corner_radius);
            }
            for (ix=[0,1], iy=[0,1]) {
                x = ix == 0 ? -side_clearance : pcb_length+side_clearance;
                y = iy == 0 ? -side_clearance : pcb_width+side_clearance;
                sx = ix == 0 ? 1 : -1;
                sy = iy == 0 ? 1 : -1;
                o = wall/2;
                translate([x,y,0]) scale([sx,sy,1]) linear_extrude(height=seat)
                    polygon([[-o,-o],[boss_leg+o,-o],[-o,boss_leg+o]]);
            }
        }
        for (x=[inset_x,pcb_length-inset_x],y=[inset_y,pcb_width-inset_y]) {
            depth = min(pilot_depth,clearance);
            translate([x,y,seat-depth]) cylinder(h=depth+0.02,d=pilot_diameter);
            translate([x,y,seat-mouth_chamfer])
                cylinder(h=mouth_chamfer+0.02,d1=pilot_diameter,
                         d2=pilot_diameter+2*mouth_chamfer+0.04);
        }
    }
}
housing();
''']
    (output_dir / "pico_plus_housing.scad").write_text("\n".join(lines), encoding="utf-8")


def preview(mesh, p, output_dir=OUT):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    # Orthographic software rendering uses a depth buffer for reliable occlusion
    # of the long, coplanar triangles on the walls of this open tray.
    width, height = 1600, 1200
    pixels = np.full((height, width, 3), (245, 243, 237), dtype=np.uint8)
    depth = np.full((height, width), -np.inf)
    az, el = math.radians(-58), math.radians(44)
    toward = np.array((math.cos(az)*math.cos(el), math.sin(az)*math.cos(el), math.sin(el)))
    right = np.cross((0, 0, 1), toward)
    right /= np.linalg.norm(right)
    up = np.cross(toward, right)
    projected = mesh.vertices @ np.stack((right, up, toward)).T
    low, high = projected[:, :2].min(axis=0), projected[:, :2].max(axis=0)
    scale = min((width-100)/(high[0]-low[0]), (height-100)/(high[1]-low[1]))
    projected[:, 0] = (projected[:, 0]-(low[0]+high[0])/2)*scale+width/2
    projected[:, 1] = height/2-(projected[:, 1]-(low[1]+high[1])/2)*scale
    light = np.array((-0.4, -0.5, 0.75))
    light /= np.linalg.norm(light)
    base = np.array((45, 131, 140))
    for face, normal in zip(mesh.faces, mesh.face_normals):
        if normal@toward <= 0:
            continue
        a, b, c = projected[face]
        xmin = max(0, int(min(a[0], b[0], c[0])))
        xmax = min(width-1, int(math.ceil(max(a[0], b[0], c[0]))))
        ymin = max(0, int(min(a[1], b[1], c[1])))
        ymax = min(height-1, int(math.ceil(max(a[1], b[1], c[1]))))
        denominator = (b[1]-c[1])*(a[0]-c[0])+(c[0]-b[0])*(a[1]-c[1])
        if abs(denominator) < 1e-10:
            continue
        yy, xx = np.mgrid[ymin:ymax+1, xmin:xmax+1]
        u = ((b[1]-c[1])*(xx+0.5-c[0])+(c[0]-b[0])*(yy+0.5-c[1]))/denominator
        v = ((c[1]-a[1])*(xx+0.5-c[0])+(a[0]-c[0])*(yy+0.5-c[1]))/denominator
        t = 1-u-v
        zz = u*a[2]+v*b[2]+t*c[2]
        section = depth[ymin:ymax+1, xmin:xmax+1]
        visible = (u >= -1e-7) & (v >= -1e-7) & (t >= -1e-7) & (zz > section)
        section[visible] = zz[visible]
        shade = 0.55+0.45*max(0, float(normal@light))
        pixels[ymin:ymax+1, xmin:xmax+1][visible] = (base*shade).astype(np.uint8)

    fig = plt.figure(figsize=(15, 8.8), facecolor="#f5f3ed")
    ax = fig.add_axes((0.005, 0.10, 0.70, 0.74))
    ax.imshow(pixels)
    ax.set_axis_off()

    fig.text(0.055, 0.935, "PICO PLUS / OPEN-TOP HOUSING", fontsize=23,
             weight="bold", color="#193a3d")
    fig.text(0.055, 0.895, "EP-0172  ·  Full-depth tray with four integrated screw bosses",
             fontsize=12, color="#536363")
    gap = p["under_pcb_clearance"]
    fig.text(0.73, 0.74, f'{gap/25.4:g}" / {gap:g} mm', fontsize=26, weight="bold", color="#193a3d")
    fig.text(0.73, 0.695, "Inside floor to PCB underside", fontsize=12, color="#536363")
    outer_l = p["pcb_length"]+2*(p["side_clearance"]+p["wall"])
    outer_w = p["pcb_width"]+2*(p["side_clearance"]+p["wall"])
    outer_h = p["floor"]+gap+p["pcb_thickness"]
    fig.text(0.73, 0.59, f"{outer_l:g} × {outer_w:g} × {outer_h:g} mm", fontsize=16, weight="bold", color="#193a3d")
    fig.text(0.73, 0.55, "Outside size with current parameters", fontsize=11, color="#536363")
    fig.text(0.73, 0.44, "4 triangular corner bosses", fontsize=16, weight="bold", color="#193a3d")
    fig.text(0.73, 0.40, "M2.5 screws · 2.2 mm pilot holes", fontsize=11, color="#536363")
    fig.text(0.73, 0.29, f'{p["hole_spacing_x"]:g} × {p["hole_spacing_y"]:g} mm hole spacing', fontsize=14, weight="bold", color="#193a3d")
    fig.text(0.73, 0.245, "Measured on your board.\nShallow fit-check STL included.",
             fontsize=11, linespacing=1.5, color="#536363")
    fig.text(0.055, 0.04, "Board seats in the open top; remove the supplied acrylic base and standoffs.",
             fontsize=11, color="#536363")
    fig.savefig(output_dir / "housing_preview.png", dpi=160, facecolor=fig.get_facecolor())
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUT,
                        help="Destination folder; use a separate folder for each variant")
    for k, v in DEFAULTS.items():
        parser.add_argument("--"+k.replace("_", "-"), type=float, default=v)
    p = vars(parser.parse_args())
    output_dir = p.pop("output_dir").resolve()
    for k, value in p.items():
        assert value > 0, (k, value)
    assert p["hole_spacing_x"] < p["pcb_length"]
    assert p["hole_spacing_y"] < p["pcb_width"]
    assert p["boss_leg"] > p["pilot_diameter"]+4
    output_dir.mkdir(parents=True, exist_ok=True)
    main_solid = build(p, p["under_pcb_clearance"])
    mesh, full = validate(main_solid, output_dir/"pico_plus_housing.stl", p,
                          p["under_pcb_clearance"])
    # A bottomless, 10 mm tall test rim saves material and leaves underside
    # components free while preserving the full 8 mm pilot depth for screw fit.
    coupon_p = {**p, "floor": 0.0}
    _, coupon = validate(build(coupon_p, 10.0), output_dir/"pico_plus_fit_check.stl", coupon_p, 10.0)
    write_scad(p, output_dir)
    preview(mesh, p, output_dir)
    report = {
        "units": "mm",
        "fit_status": "Owner measured hole spacing; physical print fit not yet tested",
        "dimension_provenance": {
            "pcb_outline": "Manufacturer assembled-dimensions image: 144 x 78 mm; verify bare PCB fit",
            "hole_spacing": "Owner measurement: 138 x 72 mm, centered on assumed outline",
            "pcb_thickness": f'Nominal {p["pcb_thickness"]:g} mm assumption; affects flush rim only, not {p["under_pcb_clearance"]:g} mm clearance',
        },
        "parameters": p,
        "mounting_centers_mm_from_assumed_pcb_lower_left": hole_centers(p),
        "mesh_checks": [full, coupon],
        "sources": [
            "https://wiki.52pi.com/index.php?title=EP-0172",
            "https://wiki.52pi.com/images/7/7e/EP-0127-05.jpg",
        ],
    }
    (output_dir/"validation.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
