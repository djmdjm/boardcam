# KiCad board CAM tool

This is a small tool that I use to assist manufacturing of front panels
for PCBs designed in KiCad. It processes a `.kicad_pcb` file and generates
cutouts for specific components that are to appear on the front panel,
such as LEDs, displays, switches, pots, etc.

Various output formats are supported, including SVG (which can be used
for laser-cutting or as the basis for artwork), OpenSCAD, GCode for a
CNC mill or a CSV list of drill hits/cuts for manual machining.

Most of my front panels are for eurorack synthesisers, so some of the
output modes (e.g. GCode) make strong assumptions about panel dimensions,
etc.

## Example usage

Generate SVG artwork from a board file:

```
$ ./board_cam.py --format=svg ~/projects/midicv/midicv.kicad_pcb > midicv.svg
```

Generate GCode to machine a eurorack front panel, excluding one component:

```
$ ./board_cam.py --skip_components=SW2 \
      --format=gcode ~/projects/midicv/midicv.kicad_pcb > midicv.nc
```

## Dependencies

This tool requires Python 3.x, KiCad and its associated Python support to
be installed.

If you want to use the SVG output mode then the
[svgwrite](https://pypi.org/project/svgwrite/) Python module must also be
installed.

## Footprints

This tool matches components found on the PCB against a list of footprints
that are considered for inclusion on the front panel. These are contained
in the `footprints.def` file. The format is hopefully fairly obvious.

Footprints currently may have either round or rectangular cutouts that may
be optionally offset of KiCad's footprint origin. If adding footprints to
this file, then care must be taken around dimensions and offsets - many
KiCad footprint modules do not place the elements of interest for a front
panel at the footprint origin.

## GCode output

This tool may be used to generate GCode to automatically machine a front
panel. At the moment, the GCode output module assumes eurorack dimensions
and will automatically size and locate the board to suit.

The GCode processor uses a list of tools `tools.cfg` to specify drills for
various sized holes, as well as a milling tool for slotting operations.
The processor will generate slotting code for rectangular cutouts as well
as for circular holes for which no drill matches. Feeds and speeds can be
specified per-tool, and various coolant modes are supported.

Drill hits may be pre-drilled using a small bit (I use a 2mm stub bit)
before being processed with the full-size drill. All drill hits are emitted
using chip-breaking canned cycles (G73) with fairly conservative parameters.
Slotting is performed with cutter compensation enabled.

The GCode output has only been tested with my mill controller (Centroid
Acorn), but it doesn't make use of anything too fancy. Do take care
of course - nobody likes a mill crash.

