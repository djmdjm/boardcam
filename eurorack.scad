// Copyright (c) 2019 Damien Miller
//
// Permission to use, copy, modify, and distribute this software for any
// purpose with or without fee is hereby granted, provided that the above
// copyright notice and this permission notice appear in all copies.
//
// THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
// WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
// MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
// ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
// WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
// ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
// OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

// Parametric model for Eurorack front panels.

$fn=60;

// Generates cylinders at each corner of a cuboid for rounding via CSG.
module corner_rounds(x, y, z, r) {
	translate([r, r, 0]) cylinder(h=z, r=r, center=false);
	translate([x-r, r, 0]) cylinder(h=z, r=r, center=false);
	translate([r, y-r, 0]) cylinder(h=z, r=r, center=false);
	translate([x-r, y-r, 0]) cylinder(h=z, r=r, center=false);
}

// Generates a triangular prism at the origin of the specified dimensions.
module triprism(l, w, h) {
	points = [
		[ 0, 0, 0 ], [ l, 0, 0 ], [ l, w, 0 ],
		[ 0, 0, h ], [ l, 0, h ], [ l, w, h ],
	];
	faces = [
		[ 0, 1, 2 ],	// bottom
		[ 0, 3, 4, 1],	// front
		[ 1, 4, 5, 2 ],	// right
		[ 2, 5, 3, 0 ],	// left
		[ 5, 4, 3 ],	// top
	];
	polyhedron(points, faces, convexity=2);
}

// Generates triangular prism at the corners of a cuboid. Used to cut the
// square corners off so they can be replaced with corner_rounds()
module corner_cuts(x, y, z, r) {
	translate([0, r, 0]) rotate([0, 0, 270]) triprism(r, r, z);
	translate([r, y, 0]) rotate([0, 0, 180]) triprism(r, r, z);
	translate([x, y-r, 0]) rotate([0, 0, 90]) triprism(r, r, z);
	translate([x-r, 0, 0]) rotate([0, 0, 0]) triprism(r, r, z);
}

// Generates a cuboid with the X/Y corners rounded to the specified radius.
module rounded_flat(x, y, z, r) {
	union() {
		difference() {
			cube([x,y,z]);
			corner_cuts(x, y, z, r);
		}
		corner_rounds(x, y, z, r);
	}
}

// Generates a blank Eurorack panel of the specified HP width and with the
// requested corner rounding.
module eurorack_panel(hp = 8, hole_dia = 3.2, r = 0.3) {
	width = 5.08 * hp;
	height = 128.5;
	depth = 2;
	difference() {
		rounded_flat(width, height, depth, r);
		// cut screw holes.
		translate([7.5, 3.0, 0])
			cylinder(h=depth, r=hole_dia / 2, center=false);
		translate([7.5, 125.5, 0])
			cylinder(h=depth, r=hole_dia / 2, center=false);
		translate([width-7.5, 3.0, 0])
			cylinder(h=depth, r=hole_dia / 2, center=false);
		translate([width-7.5, 125.5, 0])
			cylinder(h=depth, r=hole_dia / 2, center=false);
	}
}

eurorack_panel();
