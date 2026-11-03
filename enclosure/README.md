# Pico Breadboard Kit Plus housing

Open-top housing for the GeeekPi / 52Pi EP-0172 Plus. The PCB drops vertically
into the top and rests on four full-height triangular corner bosses. The PCB
underside sits **50.8 mm (2 inches) above the inside floor**. The corner bosses
occupy the corners of this space.

This is an enclosure experiment, with generated geometry checks but no recorded
physical print/board-fit test. The source is
[`generate_housing.py`](generate_housing.py). Run it to create these local files
in `../output/housing/`; generated assets under `output/` are not committed:

- `pico_plus_housing.stl`: full housing, 150.6 × 84.6 × 55.4 mm.
- `pico_plus_fit_check.stl`: bottomless test rim, 11.6 mm high, with the same
  opening, triangular pads, mounting centers, and screw pilots. It checks the
  board fit without printing the whole box.
- `pico_plus_housing.scad`: editable OpenSCAD source; set `part="fit_check"`
  for the test rim.
- `housing_preview.png`: rendering of the actual housing mesh.
- `validation.json`: dimensions and mesh validation results.

An additional **1-inch clearance version** can be generated in `../output/housing_1inch/`,
with the same filenames. It provides **25.4 mm** from the inside floor to the
PCB underside and measures **150.6 × 84.6 × 30.0 mm** outside. It uses the same
enlarged opening, triangular bosses, hole spacing, screw pilots, and fasteners.

## Dimensions and fit

- Owner-measured mounting-hole spacing: **138 × 72 mm**, centered on the board.
- Manufacturer nominal outline: **144 × 78 mm**. This comes from the assembled
  kit dimension image; verify against the bare PCB with the fit-check print.
- Assumed PCB thickness: **1.6 mm**, placing the PCB top flush with the rim.
  A different thickness changes the flush fit, but the support height still
  gives 50.8 mm below the PCB underside.
- Opening: **145.8 × 79.8 mm**, giving 0.9 mm clearance on each side.
  Length and width are each 1 mm larger than the first version, centered on
  the same mounting-hole pattern.
- Floor: **3 mm**; walls: **2.4 mm**.
- Triangular corner supports: **12 mm legs** along the inside walls.
- Screw pilots: **2.2 mm diameter, 8 mm deep**, with a small lead-in chamfer.

Physical fit has not been tested. Print the test rim first and check the board
outline, all four mounting centers, and clearance under the PCB at each pad.
The test rim has an open center, allowing underside components to hang freely.

## Printing and assembly

1. Import in **millimetres** and print at **100% scale**, with the bottom flat on
   the bed and the opening facing up. The geometry requires no supports.
2. A reasonable starting point is a 0.4 mm nozzle, 0.2 mm layers, four walls,
   and 15–20% infill. PLA or PETG is suitable for a bench housing.
3. Remove the kit's supplied acrylic base and brass standoffs.
4. Lower the PCB onto the four triangular supports. The screen and controls
   remain exposed above the opening.
5. Use **four M2.5 × 8 mm pan-head screws**. The 2.2 mm pilots are intended for
   gently forming threads in the plastic; an M2.5 tap can also be used. Check
   screw fit on the test rim and avoid over-tightening. Through a 1.6 mm PCB,
   8 mm screws engage about 6.4 mm. Longer screws may bottom out.

## Regenerating

`generate_housing.py` requires Python with numpy, manifold3d, trimesh, and
matplotlib. OpenSCAD is not required to generate the STLs. It generates both
STL files, matching OpenSCAD source, a preview, and a validation report. Run
from the repository root in a Python environment with those dependencies:

```text
python -m pip install numpy manifold3d trimesh matplotlib
python enclosure/generate_housing.py
```

Dimensions can be changed with command-line options, for example
`--pcb-length 144 --pcb-width 78 --hole-spacing-x 138 --hole-spacing-y 72`.
The OpenSCAD source is also independently editable.

To regenerate the 1-inch version in its separate output folder:

```text
python enclosure/generate_housing.py --under-pcb-clearance 25.4 --output-dir output/housing_1inch
```

The generator's checks reload each exported STL and verify watertightness, consistent
normals, one connected body, overall size, no PCB insertion collision, an empty
central clearance volume, and the four boss support planes.
They validate the model against its assumed dimensions; they do not establish
print tolerance, fastener strength, or clearance for the completed wiring and
battery/relay installation.

Sources: [manufacturer EP-0172 documentation](https://wiki.52pi.com/index.php?title=EP-0172)
and [manufacturer dimension image](https://wiki.52pi.com/images/7/7e/EP-0127-05.jpg).
Hole spacing was supplied by the owner in this chat.
