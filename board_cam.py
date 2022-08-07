#!/usr/bin/python3

# Copyright (c) 2019 Damien Miller
#
# Permission to use, copy, modify, and distribute this software for any
# purpose with or without fee is hereby granted, provided that the above
# copyright notice and this permission notice appear in all copies.
#
# THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
# WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
# ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
# WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
# ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
# OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

# Parses a .kicad_pcb file for through-panel components and emits them in a
# few useful formats (SVG, sorted list of drill hits for manual front-panel
# machining, OpenSCAD, Gcode, etc). All through-panel components must
# be represented in the footprints.def file.

from __future__ import print_function
import os
import sys
import math
import pcbnew
import argparse
import operator
import csv
import re

import gcode

# Optional extras
try:
	import svgwrite
except ImportError:
	pass

class ComponentParams(object):
	"""Records parameters for a footprint with a through panel hole"""
	__slots__ = [
		"name", "xoffset", "yoffset", "hole_dia",
		"rect_width", "rect_height", "permit_back",
	 ]
	_name_match = re.compile(r'^"([^"]+)"\s*(.*)$')
	_arg_match = re.compile(r'^(?:([a-z][a-z_]*)(\([^\)]+\))?)(?:\s*(.*))?')
	_kwargs = {
		"offset" : 2,
		"hole" : 1,
		"rect" : 2,
		"permit_back": 0,
	}
	def __init__(self, name, xoffset=0.0, yoffset=0.0, hole_dia=None, rect_width=None, rect_height=None, permit_back=False):
		self.name = name
		self.xoffset = float(xoffset)
		self.yoffset = float(yoffset)
		self.hole_dia = None
		self.rect_width = None
		self.rect_height = None
		self.permit_back = permit_back
		if hole_dia is not None:
			self.hole_dia = float(hole_dia)
		if rect_width is not None:
			self.rect_width = float(rect_width)
			self.rect_height = float(rect_height)
	@classmethod
	def parse_float_arg(cls, s):
		if not s or not s.startswith('(') or not s.endswith(')'):
			return None
		try:
			return tuple(float(p.strip()) for p in s[1:-1].split(","))
		except ValueError:
			return None

	@classmethod
	def load_line(cls, path, lnum, l):
		if not l or l.startswith('#'):
			return None
		m = cls._name_match.match(l)
		if not m or len(m.groups()) != 2:
			print("{}:{}: syntax error".format(path, lnum),
			    file=sys.stderr)
			sys.exit(1)
		rest = m.groups()[1]
		name = m.groups()[0]
		xoffset = 0.0
		yoffset = 0.0
		rect_width = None
		rect_height = None
		hole_dia = None
		permit_back = False
		while rest:
			rest = rest.strip()
			if not rest or rest.startswith("#"):
				break
			m = cls._arg_match.match(rest)
			if not m:
				print("{}:{}: parameter syntax error".
				    format(path, lnum),
				    file=sys.stderr)
				sys.exit(1)
			kw = m.groups()[0]
			rest = m.groups()[2]
			if not kw in cls._kwargs:
				print("{}:{}: unknown argument {}".
				    format(path, lnum, kw),
				    file=sys.stderr)
				sys.exit(1)
			args = cls.parse_float_arg(m.groups()[1])
			if ((args == None) != (cls._kwargs[kw] == 0)) or \
			    (args and cls._kwargs[kw] != len(args)):
				print("{}:{}: argument syntax error".
				    format(path, lnum),
				    file=sys.stderr)
				sys.exit(1)
			if kw == "offset":
				xoffset = args[0]
				yoffset = args[1]
			elif kw == "rect":
				rect_width = args[0]
				rect_height = args[1]
			elif kw == "hole":
				hole_dia = args[0]
			elif kw == "permit_back":
				permit_back = True
		if (hole_dia is None) == (rect_width is None):
			print("{}:{}: must specify hole() or rect()".
			    format(path, lnum), file=sys.stderr)
			sys.exit(1)
		return cls(name, xoffset, yoffset, hole_dia,
		    rect_width, rect_height, permit_back)

	@classmethod
	def load(cls, path):
		"""Load components from a file"""
		h = open(path)
		lnum = 0
		ret = []
		for l in h:
			lnum += 1
			params = cls.load_line(path, lnum, l.strip())
			if not params:
				continue
			ret.append(params)
		if not ret:
			print("{}: no footprints in file".
			    format(path), file=sys.stderr)
			sys.exit(1)
		return ret

class PanelComponent(object):
	"""Represents an instance of a through-panel component on a board"""
	__slots__ = [
		"params", "reference", "footprint",
		"pos_x", "pos_y", "orient",
		"hole_x", "hole_y", "hole_dia",
		"rect_x1", "rect_y1", "rect_x2", "rect_y2",
	]
	__COMPONENTS_MAP = {}

	@classmethod
	def add_known(cls, cp):
		cls.__COMPONENTS_MAP[cp.name] = cp

	@classmethod
	def known(cls, module):
		fpid = module.GetFPID().GetUniStringLibId()
		return fpid in cls.__COMPONENTS_MAP

	@classmethod
	def pemitted_on_back(cls, module):
		fpid = module.GetFPID().GetUniStringLibId()
		if not fpid in cls.__COMPONENTS_MAP:
			return False
		return cls.__COMPONENTS_MAP[fpid].permit_back

	@classmethod
	def _sortkey(cls, attrs, item):
		vals = operator.attrgetter(*attrs)(item)
		ret = []
		for r in vals:
			if r is None:
				r = 0.0 # XXX hack
			ret.append(r)
		return tuple(ret)

	@classmethod
	def sort_components(cls, components, sort=None):
		if sort is None:
			return components
		kk = sort.split(",")
		for key in kk:
			if key not in cls.__slots__:
				raise ValueError("unknown sort key: {}". \
				    format(key))
		#return sorted(components, key=operator.attrgetter(*kk))
		return sorted(components, key=lambda v: cls._sortkey(kk, v))

	@classmethod
	def _transform(cls, x, y, theta):
		xt = (x * math.cos(math.radians(theta))) + y * math.cos(math.radians(270.0 + theta))
		yt = (y * math.cos(math.radians(theta))) + -x * math.cos(math.radians(270.0 + theta))
		return xt, yt

	def __init__(self, module=None, board_edge_x=None, board_edge_y=None,
	    reference=None, footprint=None, pos_x=None, pos_y=None,
	    orient=None, adjustments=None, verbose=0):
		if module is not None:
			fpid = module.GetFPID().GetUniStringLibId()
			self.reference = module.GetReference()
			self.pos_x = pcbnew.ToMM(module.GetPosition().x)
			self.pos_y = pcbnew.ToMM(module.GetPosition().y)
			self.orient = module.GetOrientation() / 10.0
		else:
			if reference is None or footprint is None or \
			    pos_x is None or pos_y is None or orient is None:
				raise ValueError("missing arguments")
			fpid = footprint
			self.reference = reference
			self.pos_x = pos_x
			self.pos_y = pos_y
			self.orient = orient
		if adjustments is not None and self.reference in adjustments:
			adj = adjustments[self.reference]
			self.pos_x += adj[0]
			self.pos_y += adj[1]
		self.hole_x = None
		self.hole_y = None
		self.hole_dia = None
		self.rect_x1 = None
		self.rect_y1 = None
		self.rect_x2 = None
		self.rect_y2 = None
		if board_edge_x is not None:
			self.pos_x -= board_edge_x
		if board_edge_y is not None:
			self.pos_y -= board_edge_y
		if not fpid in self.__class__.__COMPONENTS_MAP:
			raise ValueError("No footprint for \"{}\"".format(fpid))
		self.params = self.__class__.__COMPONENTS_MAP[fpid]
		self.footprint = fpid
		# Calculate hole position.
		xoff, yoff = self._transform(self.params.xoffset, self.params.yoffset, self.orient)
		if self.params.hole_dia is not None:
			self.hole_x = self.pos_x + xoff
			self.hole_y = self.pos_y + yoff
			self.hole_dia = self.params.hole_dia
		elif self.params.rect_width is not None:
			self.rect_x1 = self.pos_x + xoff
			self.rect_y1 = self.pos_y + yoff
			width, height = self._transform(self.params.rect_width, self.params.rect_height, self.orient)
			self.rect_x2 = self.rect_x1 + width
			self.rect_y2 = self.rect_y1 + height
		else:
			raise ValueError("unknown cutout: {}". format(self))

	def __str__(self):
		ret = "{}: at(x={}, y={}, r={})".format(self.reference,
		    self.pos_x, self.pos_y, self.orient)
		if self.hole_dia is not None:
			ret += " hole(x={}, y={}, d={})".format(
			    self.hole_x, self.hole_y, self.hole_dia)
		elif self.rect_x1 is not None:
			ret += " rect(x1={}, y1={}, x2={}, y2={})".format(
			    self.rect_x1, self.rect_y1,
			    self.rect_x2, self.rect_y2)
	def __repr__(self):
		return ("{}(reference={!r}. footprint={!r}, pos_x={}, "+
		    "pos_y={}, orient={})").format(self.__class__.__name__,
		    self.reference, self.footprint, self.pos_x, self.pos_y,
		    self.orient)

class PanelBoard:
	"""Represents the features of interest from a kicad PCB"""
	__slots__ = [
		"filename", "components", "bounds", "board_bounds",
		"board_width", "board_height",
		"skip_components", "include_components",
		"adjust_components",
	]

	def __init__(self, filename, board,
	    board_edge_x=None, board_edge_y=None,
	    board_height=None, board_width=None,
	    skip_components=None, include_components=None,
	    adjust_components=None, sort=None, verbose=0):
		self.filename = filename
		self.components = []
		self.board_bounds = None
		self.board_width = board_width
		self.board_height = board_height
		self.bounds = None
		self.skip_components = skip_components
		self.include_components = include_components
		self.adjust_components = adjust_components
		# Gather list of interesting footprints and their boundaries.
		footprints = []
		self.bounds = None
		if hasattr(board, 'GetModules'):
			modules = board.GetModules()
		else:
			modules = board.GetFootprints()
		for footprint in modules:
			if not self.keep_footprint(footprint, verbose):
			    continue
			footprints.append(footprint)
			if self.bounds is None:
				self.bounds = footprint.GetBoundingBox()
			else:
				self.bounds.Merge(footprint.GetBoundingBox())

		if len(footprints) == 0:
			return

		# Determine board outline that encloses the selected components.
		board_polys = self.find_edge_polys(board, verbose)
		for poly in board_polys:
			if poly.Intersects(self.bounds):
				origin = self.wxpt_to_mm(poly.GetOrigin())
				end = self.wxpt_to_mm(poly.GetEnd())
				print(("detected usable board edge "+
				    "({:.3f},{:.3f} - {:.3f},{:.3f}) "+
				    "size {:.3f}x{:.3f}").format(
				    origin[0], origin[1], end[0], end[1],
				    pcbnew.ToMM(poly.GetSize().GetWidth()),
				    pcbnew.ToMM(poly.GetSize().GetHeight())),
				    file=sys.stderr)
				self.board_bounds = poly
				break
		if self.board_bounds is None:
			if board_edge_x is None and board_edge_y is None:
				print("WARNING: could not auto-detect board "+
				    "edge; specify manually if required")
		else:
			board_edge = self.wxpt_to_mm(
			    self.board_bounds.GetOrigin())
			if board_edge_x is None:
				board_edge_x = board_edge[0]
			if board_edge_y is None:
				board_edge_y = board_edge[1]
			board_size = self.wxpt_to_mm(
			    self.board_bounds.GetSize())
			if self.board_width is None:
				self.board_width = board_size[0]
			if self.board_height is None:
				self.board_height = board_size[1]
		components = []
		for footprint in footprints:
			components.append(PanelComponent(footprint,
			    board_edge_x=board_edge_x,
			    board_edge_y=board_edge_y,
			    adjustments=self.adjust_components,
			))
		self.components = PanelComponent.sort_components(
		    components, sort=sort)


	def keep_footprint(self, footprint, verbose):
		fpid = footprint.GetFPID().GetUniStringLibId()
		ref = footprint.GetReference()
		if not PanelComponent.known(footprint):
			if verbose >= 1:
				print("ignored unknown", ref, fpid,
				    file=sys.stderr)
			return False
		if footprint.GetLayer() != 0 and \
		    not PanelComponent.pemitted_on_back(footprint):
			if verbose >= 2:
				print("ignored back side", ref, fpid,
				    file=sys.stderr)
			return False
		if self.skip_components and ref in self.skip_components:
			if verbose >= 1:
				print("skipped", ref, fpid, file=sys.stderr)
			return False
		if self.include_components and \
		   not ref in self.include_components:
			if verbose >= 1:
				print("excluded", ref, fpid, file=sys.stderr)
			return False
		return True

	def wxpt_to_mm(self, p):
		return pcbnew.ToMM(p.x), pcbnew.ToMM(p.y)

	def find_edge_polys(self, board, verbose=0):
		"""Find the set of contiguous board outline polygons. Returns
		   a list of line start+end points."""
		edges = []
		for dwg in board.GetDrawings():
			if (hasattr(pcbnew, "PCB_LINE_T") and
			   dwg.Type() != pcbnew.PCB_LINE_T):
				continue
			elif dwg.Type() != pcbnew.PCB_SHAPE_T:
				continue
			if dwg.GetLayer() != pcbnew.Edge_Cuts:
				continue
			edges.append(dwg)
		if len(edges) == 0:
			return []
		# Find coincident edge groups.
		by_coord = {}
		for edge in edges:
			if edge.Type() == pcbnew.SH_POLY_SET and \
			   edge.GetPolyShape().OutlineCount() != 0:
				chain = edge.GetPolyShape().COutline(0)
				last = None
				for i in range(chain.GetPointCount()):
					pt = chain.GetPoint(i)
					cur = self.wxpt_to_mm(pt)
					if verbose >= 6:
						print("polyset: point",
						    cur, file=sys.stderr)
					if cur not in by_coord:
						by_coord[cur] = []
					if last is not None:
						by_coord[last].append(
						    (last, cur))
						if verbose >= 4:
							print("polyset: "
							    "line from",
							    last, "to", cur,
							    file=sys.stderr)
					last = cur
			else:
				start = self.wxpt_to_mm(edge.GetStart())
				end = self.wxpt_to_mm(edge.GetEnd())
				if start not in by_coord:
					by_coord[start] = []
				if end not in by_coord:
					by_coord[end] = []
				by_coord[end].append((start, end))
				if start != end:
					by_coord[end].append((start, end))
		contiguous = {}
		while len(by_coord) != 0:
			worklist = set([next(iter(by_coord))])
			if verbose >= 4:
				print("top: contig", contiguous,
				    "work", worklist, file=sys.stderr)
			region_key = None
			while len(worklist) != 0:
				# Take the first coordinate on the worklist.
				lines_key = next(iter(worklist))
				if verbose >= 4:
					print("work: line", lines_key,
					    "region", region_key,
					    "work", worklist, file=sys.stderr)
				worklist.remove(lines_key)
				if not lines_key in by_coord:
					continue
				lines = by_coord[lines_key]
				if verbose >= 4:
					print("work: line", lines_key,
					    "candidates", lines,
					    file=sys.stderr)
				# Record all the lines that start or end at
				# this point as a contiguous region.
				del by_coord[lines_key]
				if region_key is None and len(lines) > 0:
					region_key = lines[0]
				if region_key not in contiguous:
					if verbose >= 4:
						print("work: line", lines_key,
						    "start region", region_key,
						    file=sys.stderr)
					contiguous[region_key] = set()
				for line in lines:
					contiguous[region_key].add(line)
					# Continue working from the points at
					# the other end of lines radiating
					# from the point under consideration.
					worklist.add(line[0])
					worklist.add(line[1])
		if verbose >= 4:
			print("done; contig", contiguous, file=sys.stderr)
		rects = []
		for poly in contiguous.values():
			xmin, xmax, ymin, ymax = None, None, None, None
			if verbose >= 5:
				print("contig poly", poly, file=sys.stderr)
			for line in poly:
				if verbose >= 6:
					print("contig line", line,
					    file=sys.stderr)
				for point in line:
					if verbose >= 6:
						print("contig lpoint",
						    point, file=sys.stderr)
					if xmin is None or point[0] < xmin:
						xmin = point[0]
					if ymin is None or point[1] < ymin:
						ymin = point[1]
					if xmax is None or point[0] > xmax:
						xmax = point[0]
					if ymax is None or point[1] > ymax:
						ymax = point[1]
			if verbose >= 3:
				print("poly", xmin, ymin, xmax, ymax,
				    file=sys.stderr)
			if None in (xmin, ymin, xmax, ymax):
				continue
			rect = pcbnew.EDA_RECT()
			rect.SetOrigin(pcbnew.FromMM(xmin), pcbnew.FromMM(ymin))
			rect.SetEnd(pcbnew.FromMM(xmax), pcbnew.FromMM(ymax))
			rect.Normalize()
			rects.append(rect)
		return rects

def load_footprint_definitions(path):
	params = None
	try:
		params = ComponentParams.load(path)
	except FileNotFoundError:
		pass
	if params is None:
		try:
			params = ComponentParams.load(
			    os.path.join(sys.path[0], path))
		except (IndexError, FileNotFoundError):
			pass
	if params is None:
		print("Cannot find component parameters {}".format(path),
		    file=sys.stderr)
		sys.exit(1)
	for footprint in params:
		PanelComponent.add_known(footprint)

def process_file(filename, board_edge_x, board_edge_y,
    skip_components=None, include_components=None, adjust_components=None,
    sort=None, verbose=0):
	"""Parse a .kicad_pcb file to a sorted list of PanelComponents"""
	board = pcbnew.LoadBoard(filename)
	return PanelBoard(filename, board, board_edge_x, board_edge_y,
	    skip_components=skip_components,
	    include_components=include_components,
	    adjust_components=adjust_components,
	    sort=sort, verbose=verbose)

def output_tabular(args, panelbrd):
	n = 1
	for component in panelbrd.components:
		if component.hole_dia is not None:
			print("{:3d}: {:6} drill {:9.3f} {:9.3f} dia {:4.2f}" \
			    .format(n, component.reference, component.hole_x,
			    component.hole_y, component.hole_dia))
		elif component.rect_x1 is not None:
			print("{:3d}: {:6} rect {:9.3f} {:9.3f} {:9.3f} {:9.3f}" \
			    .format(n, component.reference,
			    component.rect_x1, component.rect_y1,
			    component.rect_x2, component.rect_y2))
		n += 1

def svg_mm(p):
	return "{:.3f}mm".format(p)

def output_csv(args, panelbrd,
    fields="reference,footprint,pos_x,pos_y,hole_x,hole_y,hole_dia,rect_x1,rect_y1,rect_x2,rect_y2"):
	kk = fields.split(",")
	for key in kk:
		if key not in PanelComponent.__slots__:
			raise ValueError("unknown field: {}". format(key))
	dw = csv.DictWriter(sys.stdout, fieldnames=kk, extrasaction="ignore")
	dw.writeheader()
	for component in panelbrd.components:
		attrs = {}
		for f in kk:
			attrs[f] = component.__getattribute__(f)
		dw.writerow(attrs)

def output_svg(args, panelbrd, width=None, height=None, xoff=0.0, yoff=0.0,
    hole_size=0.8, graticule_size=0.5, text_yoff=2.5):
	if "svgwrite" not in sys.modules:
		raise ValueError("The svgwrite module is required for SVG "+
		    "output. Please install it")
	bounds = panelbrd.board_bounds
	if bounds is None:
		bounds = panelbrd.bounds
	if bounds is None and (width is None or height is None):
		raise ValueError("Cannot determine extents; no components?")
	if width is None:
		width = pcbnew.ToMM(bounds.GetSize().GetWidth())
	if height is None:
		height = pcbnew.ToMM(bounds.GetSize().GetHeight())
	print("SVG drawing size is {:.3f}x{:.3f}mm".format(width, height),
	    file=sys.stderr)
	sd = svgwrite.Drawing(filename="board.svg", profile="tiny",
	    size=(svg_mm(width), svg_mm(height)))
	for component in panelbrd.components:
		grp = sd.add(sd.g(id=component.reference))
		if component.hole_dia is not None:
			rad = component.hole_dia / 2.0
			xpos = component.hole_x + xoff
			ypos = component.hole_y + yoff
			xcentre = xpos * svgwrite.mm
			ycentre = ypos * svgwrite.mm
			# Add sized drill hole with centre graticule.
			dgrp = grp.add(sd.g(id="drill-"+component.reference))
			dgrp.add(sd.circle(center=(xcentre, ycentre),
			    r=rad * hole_size * svgwrite.mm,
			    stroke="black", stroke_width=0.2 * svgwrite.mm,
			    fill="none", id="D-"+component.reference))
			gratlen = rad * hole_size * graticule_size
			xstart = (xpos - gratlen) * svgwrite.mm
			xend = (xpos + gratlen) * svgwrite.mm
			ystart = (ypos - gratlen) * svgwrite.mm
			yend = (ypos + gratlen) * svgwrite.mm
			dgrp.add(sd.line(
			    start=(xstart, ycentre), end=(xend, ycentre),
			    stroke="black", stroke_width=0.2 * svgwrite.mm))
			dgrp.add(sd.line(
			    start=(xcentre, ystart), end=(xcentre, yend),
			    stroke="black", stroke_width=0.2 * svgwrite.mm))
			# Add text
			textpos = (svg_mm(xpos), svg_mm(ypos + rad + text_yoff))
			t = sd.text(component.reference, insert=textpos,
			    fill="black", text_anchor="middle",
			    font_family="sans-serif", font_size=8,
			    id="T-"+component.reference)
			t["xml:space"] = "preserve"
			grp.add(t)
		elif component.rect_x1 is not None:
			rect_x1 = component.rect_x1 + xoff
			rect_y1 = component.rect_y1 + yoff
			rect_x2 = component.rect_x2 + xoff
			rect_y2 = component.rect_y2 + yoff
			rect_width = component.rect_x2 - component.rect_x1
			rect_height = component.rect_y2 - component.rect_y1
			rgrp = grp.add(sd.g(id="rect-"+component.reference))
			rgrp.add(sd.rect(
			    insert=(rect_x1 * svgwrite.mm,
			    rect_y1 * svgwrite.mm),
			    size=(rect_width * svgwrite.mm,
			    rect_height * svgwrite.mm),
			    fill = "none", stroke="black",
			    stroke_width=0.2 * svgwrite.mm))

	sd.write(sys.stdout)

def output_eurorack_svg(args, panelbrd):
	bounds = panelbrd.board_bounds
	if bounds is None:
		bounds = panelbrd.bounds
	if bounds is None:
		raise ValueError("Cannot determine extents; no components?")
	width = pcbnew.ToMM(bounds.GetSize().GetWidth())
	height = pcbnew.ToMM(bounds.GetSize().GetHeight())
	if width <= 0:
		raise ValueError("Board too thin")
	if height > 110:
		raise ValueError("Board too tall for eurorack module")
	eurorack_height = 128.5
	hp = int(math.ceil(width / 5.08))
	xoff = ((5.08 * hp) - width) / 2
	yoff = (eurorack_height - height) / 2
	print("board requires {} HP ({:.2f}mm)".format(hp, hp * 5.08),
	    file=sys.stderr)
	# XXX Some sort of extra-holes mechanism to pass in eurorack fix holes.
	output_svg(args, panelbrd, width=hp * 5.08, height=eurorack_height,
	    xoff=xoff, yoff=yoff)

def output_eurorack_openscad(args, panelbrd):
	bounds = panelbrd.board_bounds
	if bounds is None:
		bounds = panelbrd.bounds
	if bounds is None:
		raise ValueError("Cannot determine extents; no components?")
	width = pcbnew.ToMM(bounds.GetSize().GetWidth())
	height = pcbnew.ToMM(bounds.GetSize().GetHeight())
	if width <= 0:
		raise ValueError("Board too thin")
	if height > 110:
		raise ValueError("Board too tall for eurorack module")
	eurorack_height = 128.5
	hp = int(math.ceil(width / 5.08))
	xoff = ((5.08 * hp) - width) / 2
	yoff = (eurorack_height - height) / 2
	print("board requires {} HP ({:.2f}mm)".format(hp, hp * 5.08),
	    file=sys.stderr)
	print("use <eurorack.scad>")
	print("")
	print("$fn=32;")
	print("$vpr = [0, 0, 0];")
	print("$vpt = [{:.2f}, {:.2f}, 0];".
	    format(hp * 5.08 / 2, eurorack_height / 2))
	print("$vpd = {:.2f};".format(max(width, height) * 2.5))
	print("depth=2;")
	print("difference() {")
	print("	eurorack_panel(hp = {});".format(hp))
	# Drills
	for component in panelbrd.components:
		if component.hole_dia is None:
			continue
		print("	// Drill: ", component.reference)
		print("	translate([{:.3f}, {:.3f}, 0])".
		    format(component.hole_x + xoff,
		    eurorack_height - (component.hole_y + yoff)))
		print("	    cylinder(h=depth, r={:.3f} / 2.0, center=false);".
		    format(component.hole_dia))
	# Rects
	for component in panelbrd.components:
		if component.rect_x1 is None:
			continue
		print(" // Rect: ", component.reference)
		print("	translate([{:.3f}, {:.3f}, 0])".
		    format(component.rect_x1 + xoff,
		    eurorack_height - (component.rect_y2 + yoff)))
		print("	    cube(size=[{:.3f}, {:.3f}, depth], center=false);".
		    format(component.rect_x2 - component.rect_x1,
		    component.rect_y2 - component.rect_y1))
	print("}")

if __name__ == '__main__':
	outputters = {
		"none": None,
		"tabular": output_tabular,
		"csv": output_csv,
		"svg": output_svg,
		"eurorack_openscad": output_eurorack_openscad,
		"eurorack_svg": output_eurorack_svg,
		"gcode": gcode.output_gcode,
	}
	parser = argparse.ArgumentParser()
	parser.add_argument("filename", help="path to .kicad_pcb board file")
	parser.add_argument("--verbose",
	    help="print increasingly verbose diagnostics", type=int,
	    default=0)
	parser.add_argument("--edge_x",
	    help="manual board edge X start coordinate (mm)", type=float)
	parser.add_argument("--edge_y",
	    help="manual board edge Y start coordinate (mm)", type=float)
	parser.add_argument("--sort",
	    help="sort order for results (comma separated list of keys: {})".
		format(", ".join(PanelComponent.__slots__)),
		default="hole_x,hole_y")
	parser.add_argument("--format", help="output format",
	    choices=outputters.keys(), default="tabular")
	parser.add_argument("--footprints_def_path",
	    help="Path to footprint definition file", default="footprints.def",
	    type=str)
	parser.add_argument("--gcode_tool_config",
	    help="Path to G-code tool configuration file", default="tools.cfg",
	    type=str)
	parser.add_argument("--gcode_cutout_panel",
	    help="G-code should include cutout of Eurorack panel",
	    default=False, type=bool)
	parser.add_argument("--gcode_mount_drill",
	    help="G-code drill size (mm) for mounting holes", default=3.4,
	    type=float)
	parser.add_argument("--skip_components",
	    help="comma-separated list of components to skip", default="",
	    type=str)
	parser.add_argument("--include_components",
	    help="comma-separated list of components to include (all others will be skipped)", default="",
	    type=str)
	parser.add_argument("--adjust_components",
	    help="comma-separated list of (ref:xoff,yoff) to adjust position for(offsets may be negative)", default="",
	    type=str)
	args = parser.parse_args()
	def parse_list(l):
		return list(filter(bool, l.strip().split(",")))
	def parse_adjustment(l):
		ret = {}
		try:
			for s in l.strip().split("),"):
				if s == "":
					continue
				item = s.lstrip("(").rstrip(")").split(":")
				ref = item[0]
				pos = item[1].split(",")
				ret[ref] = (float(pos[0]), float(pos[1]))
		except (ValueError, IndexError):
			print("Bad adjustment list", file=sys.stderr)
			sys.exit(1)
		return ret
	load_footprint_definitions(args.footprints_def_path)
	board = process_file(args.filename,
	    board_edge_x = args.edge_x, board_edge_y = args.edge_y,
	    skip_components = parse_list(args.skip_components),
	    include_components = parse_list(args.include_components),
	    adjust_components = parse_adjustment(args.adjust_components),
	    sort = args.sort, verbose = args.verbose)
	if len(board.components) == 0:
		print("No matching components found", file=sys.stderr)
		sys.exit(1)
	if args.format != "none":
		outputters[args.format](args, board)

