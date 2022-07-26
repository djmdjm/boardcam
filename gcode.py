#!/usr/bin/python3

# Copyright (c) 2020 Damien Miller
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

# G-Code output routines

import math
import time
import sys
import os
import operator
import pcbnew

_tokens = {
	# Tool definitions
	"tool":		False,
	"dia":		False,
	"downfeed":	False,
	"feed":		False,
	"speed":	False,
	"type":		False,
	# Preference definitions
	"mill":		True,
	"predrill":	True,
	"coolant":	True,
}
_validtypes = set(("drill", "endmill"))

class GCodeTool(object):
	"""Records information about a tool used in GCode generation"""
	__slots__ = [
		"num", "dia", "downfeed", "feed", "speed", "tooltype"
	 ]
	def __init__(self, num, dia, feed, speed, tooltype, downfeed=None):
		self.num = int(num)
		self.dia = float(dia)
		self.feed = float(feed)
		self.speed = float(speed)
		if tooltype not in _validtypes:
			raise ValueError("unrecognised tool type {}".
			    format(tooltype))
		self.tooltype = tooltype
		if downfeed is None:
			self.downfeed = float(feed)
		else:
			self.downfeed = float(downfeed)

class GCodeToolTable(object):
	def __init__(self, path):
		self.predrill = None
		self.mill = None
		self.coolant = "none"
		self.tools = {}
		lnum = 0
		for line in open(path):
			lnum += 1
			try:
				line = line.split('#')[0].strip()
				if line == "":
					continue
				parsed = self.parse_toolcfg_line(line)
			except ValueError as err:
				print("{}:{}: {}".format(path, lnum, err),
				    file=sys.stderr)
				sys.exit(1)
			if parsed.__class__ is GCodeTool:
				# This line is a tool definition.
				if parsed.num in self.tools:
					print("{}:{}: duplicate tool {}".
					    format(path, lnum, parsed.num),
					    file=sys.stderr)
					sys.exit(1)
				self.tools[parsed.num] = parsed
				continue
			# This line is a preference.
			k, v = parsed
			if k == "predrill":
				self.predrill = v
			elif k == "mill":
				self.mill = v
			elif k == "coolant":
				self.coolant = v
		if self.predrill is not None:
			if self.predrill not in self.tools:
				print("Predrill refers to missing tool {}".
				    format(self.predrill), file=sys.stderr)
				sys.exit(1)	
			if self.tools[self.predrill].tooltype != "drill":
				print("Predrill {} is not a drill".
				    format(self.predrill), file=sys.stderr)
				sys.exit(1)
		if self.mill is not None:
			if self.mill not in self.tools:
				print("Mill refers to missing tool {}".
				    format(self.mill), file=sys.stderr)
				sys.exit(1)	
			if self.tools[self.mill].tooltype != "endmill":
				print("Mill {} is not an endmill".
				    format(self.mill), file=sys.stderr)
				sys.exit(1)
		if self.coolant not in [ "flood", "mist", "none" ]:
			print("invalid coolant mode {}".
                    format(self.coolant), file=sys.stderr)
			sys.exit(1)

	def parse_toolcfg_line(self, line):
		tokens = {}
		# XXX separate parsers for tool / predrill / etc lines.
		for token in line.split():
			try:
				k, v = token.split("=")
			except ValueError:
				raise ValueError("syntax error")
			if k in tokens:
				raise ValueError("duplicate token {}".format(k))
			if not k in _tokens:
				raise ValueError("unsupported keyword {}".
				    format(k))
			tokens[k] = v
		for k in tokens:
			if _tokens[k]:
				# This is a preference; it should appear alone.
				if len(tokens) > 1:
					raise ValueError(("syntax error: "+
					    "{} must appear alone on line").
					    format(k))
				# XXX another ugly special-case
				if k == "coolant":
						return (k, tokens[k])
				try:
					return (k, int(tokens[k]))
				except ValueError:
					raise ValueError("invalid tool number format")
		# This is a tool definition line.
		if "downfeed" not in tokens:
			tokens["downfeed"] = None
		try:
			return GCodeTool(tokens["tool"], tokens["dia"],
			    tokens["feed"], tokens["speed"], tokens["type"],
			    tokens["downfeed"])
		except KeyError as err:
			raise ValueError(
			    "tool definition missing required attribute {}".
			     format(err))
	def drill_dias(self):
		ret = []
		for num in self.tools:
			if self.tools[num].tooltype != "drill":
				continue
			ret.append(self.tools[num].dia)
		return ret
	def drill_by_dia(self, n):
		for num in self.tools:
			if self.tools[num].tooltype != "drill":
				continue
			if self.tools[num].dia == n:
				return self.tools[num]
		return None
	def tool_by_num(self, n):
		return self.tools[n]
	def predrill_tool(self):
		if self.predrill is None:
			return None
		return self.tools[self.predrill]
	def mill_tool(self):
		if self.mill is None:
			return None
		return self.tools[self.mill]
	def all_tools(self):
		return sorted(self.tools.values(),
		    key=operator.attrgetter("num"))

class GCodeOperation(object):
	"""Tracks a single cutting hit (drill or cutout)"""
	def __init__(self, ref, footprint):
		self.ref = ref
		self.footprint = footprint

class GCodeDrillHit(GCodeOperation):
	"""Tracks a single drill hit"""
	def __init__(self, ref, footprint, dia, depth, xpos, ypos):
		self.ref = ref
		self.footprint = footprint
		self.dia = dia
		self.depth = depth
		self.xpos = xpos
		self.ypos = ypos
		self.xstart = None
		self.ystart = None
		self.need_x = dia
		self.need_y = dia
	def add_start(self, drill_dia):
		"""Adds coordinates for a starting drill"""
		# Centre
		self.xstart = self.xpos
		self.ystart = self.ypos

class GCodeRectCut(GCodeOperation):
	"""Tracks a single rectangular cutout"""
	def __init__(self, ref, footprint, depth, x1, y1, x2, y2):
		self.ref = ref
		self.footprint = footprint
		self.depth = depth
		# Top left
		self.x1 = x1
		self.y1 = y1
		# Bottom right
		self.x2 = x2
		self.y2 = y2
		if self.x1 > self.x2:
			raise ValueError(("{} bad extent ordering: "+
			    "left edge {} is greater than right edge {}").
			    format(ref, x1, x2))
		if self.y1 < self.y2:
			raise ValueError(("{} bad extent ordering: "+
			    "top edge {} is less than bottom edge {}").
			    format(ref, y1, y2))
		self.xstart = None
		self.ystart = None
		self.need_x = self.x2 - self.x1
		self.need_y = self.y1 - self.y2 # XXX correct?
	def add_start(self, drill_dia):
		"""Adds coordinates for a starting drill"""
		# Near top left.
		self.xstart = self.x1 + drill_dia
		self.ystart = self.y1 - drill_dia

class GCodeOutput(object):
	def __init__(self, panelbrd, toolcfg, cutout_panel=False,
	    mount_drill=3.2):
		self.panelbrd = panelbrd
		self.toolcfg = toolcfg
		self.cutout_panel = cutout_panel
		self.mount_drill = mount_drill
		self.lineno = 100
		self.hover = 2.0
		self.eurorack_height = 128.5
		self.eurorack_thickness = 2.0
		self.mill_depth_clearance = 0.075
		self.drill_depth_clearance = 0.1

		# Prepare the board.
		bounds = panelbrd.board_bounds
		if bounds is None:
			self.bounds = panelbrd.bounds
		if bounds is None:
			raise ValueError("Cannot determine extents; "+
			    "no components?")
		self.width = pcbnew.ToMM(bounds.GetSize().GetWidth())
		self.height = pcbnew.ToMM(bounds.GetSize().GetHeight())
		if self.width <= 0:
			raise ValueError("Board too thin")
		if self.height > 110:
			raise ValueError("Board too tall for eurorack module")
		self.hp = int(math.ceil(self.width / 5.08))
		self.eurorack_width = self.hp * 5.08
		# XXX options to tweak offset within panel
		self.xoff = ((5.08 * self.hp) - self.width) / 2
		self.yoff = (self.eurorack_height - self.height) / 2
		print("board requires {} HP ({:.2f}mm)".format(
		    self.hp, self.eurorack_width), file=sys.stderr)
		# Default sort order
		# Prepare hole lists, recording which drills are in use.
		self.drills = {}
		self.round_cutouts = []
		for component in self.panelbrd.components:
			if component.hole_dia is None:
				continue
			drillhit = GCodeDrillHit(component.reference,
			    component.footprint, component.hole_dia,
			    self.eurorack_thickness,
			    *self.xform(component.hole_x, component.hole_y))
			if self.toolcfg.drill_by_dia(drillhit.dia) is None:
				print(("{}: no drill specified for {:0.2f}mm "+
				    "diameter, will slot using mill tool").
				    format(component.reference, drillhit.dia),
				    file=sys.stderr)
				self.round_cutouts.append(drillhit)
			else:
				self.add_drill(drillhit)
		if self.toolcfg.mill_tool() is None and \
		    len(self.round_cutouts) > 0:
			raise ValueError(("No mill specified for {} "+
			    "round cutouts").format(len(self.round_cutouts)))

		self.rect_cutouts = []
		for component in self.panelbrd.components:
			if component.rect_x1 is None:
				continue
			rectcut = GCodeRectCut(component.reference,
			    component.footprint, self.eurorack_thickness,
			    *self.xform(component.rect_x1, component.rect_y1),
			    *self.xform(component.rect_x2, component.rect_y2))
			self.rect_cutouts.append(rectcut)
		if self.toolcfg.mill_tool() is None and \
		    len(self.rect_cutouts) > 0:
			raise ValueError(("No mill specified for {} "+
			    "rectangular cutouts").
			    format(len(self.rect_cutouts)))
		for rc in self.rect_cutouts + self.round_cutouts:
			drill_dia = self.start_drill(rc)
			rc.add_start(drill_dia)
			descr = "{} cutout entry hole".format(rc.ref)
			drillhit = GCodeDrillHit(descr, rc.footprint,
			    drill_dia, rc.depth, rc.xstart, rc.ystart)
			self.add_drill(drillhit)

		cutout_key = operator.attrgetter("xstart", "ystart")
		self.rect_cutouts.sort(key=cutout_key)
		self.round_cutouts.sort(key=cutout_key)

		if self.cutout_panel:
			self.add_panel_cutout()
	def xform(self, x, y):
		"""Transform a coordinate from KiCAD's system to GCode"""
		# KiCAD's Y coordinate system is the opposite of GCode.
		return (
		    x + self.xoff,
		    -y - self.yoff,
		)
	def add_drill(self, drillhit):
		"""Add a drill hit to the map (keyed by diameter)"""
		if not drillhit.dia in self.drills:
			self.drills[drillhit.dia] = []
		self.drills[drillhit.dia].append(drillhit)
	def start_drill(self, component):
		"""Determine a suitable starting drill for a cutout"""
		tool = self.toolcfg.mill_tool()
		need = min(component.need_x, component.need_y)
		if tool.dia * 2.0 > need:
			print(("Mill tool #{} dia {:0.3f} too big for "+
			    "required clearance {:0.3f}").format(
			    tool.num, tool.dia, need))
			sys.exit(1)
		# Find the smallest drill bigger than the endmill that
		# we can use to drill a starting hole with good (1r) clearance.
		candidate_drills = [dia for dia in self.toolcfg.drill_dias()
		    if dia > tool.dia and dia*2 < need]
		if len(candidate_drills) == 0:
			print(("No suitable start drill for {} (need {}) " +
			    "mill tool #{} dia {:0.3f}").format(
			    component.ref, need, tool.num, tool.dia))
			sys.exit(1)
		# Pick the smallest available drill.
		return sorted(candidate_drills)[0]
	def newblock(self):
		"""Increase the line number to a multiple of 1000"""
		self.lineno = int((self.lineno + 999) / 1000) * 1000
	def G(self, fmt, *args):
		"""Emit a numbered GCode line"""
		print("N{}".format(self.lineno), fmt.format(*args))
		self.lineno += 10
	def C(self, fmt, *args):
		"""Emit a GCode comment line"""
		print(";", fmt.format(*args))
	def coolant_on(self):
		"""Activate coolant (if requested by tool config)"""
		if self.toolcfg.coolant == "flood":
			self.G("M8")
		elif self.toolcfg.coolant == "mist":
			self.G("M7")
	def coolant_off(self):
		"""Deactivate coolant (if requested by tool config)"""
		if self.toolcfg.coolant in [ "flood", "mist" ]:
			self.G("M9")
	def output(self):
		"""Output GCode for the board"""
		# Preamble.
		self.C("Converted from {} by getpos.py at {}",
		    os.path.basename(self.panelbrd.filename),
		    time.strftime("%Y-%m-%dT%H:%M:%S %z"))
		self.C("")
		self.C("Tool list:")
		for tool in self.toolcfg.all_tools():
			self.C("    Tool {}: {:0.1f}mm {}",
			    tool.num, tool.dia, tool.tooltype)
		self.C("")
		self.C("Coolant: {}", self.toolcfg.coolant)
		self.C("")
		self.C("Origin is at top left of panel edge")
		self.C("Panel size is X: {:0.2f} ({} HP) Y: {:0.1f}",
			self.eurorack_width, self.hp, self.eurorack_height)
		self.C("PCB size is X: {:0.3f} Y: {:0.3f}",
			self.width, self.height)
		self.C("PCB offset in panel is X: {:0.3f} Y: {:0.3f}",
			self.xoff, self.yoff)
		if self.cutout_panel:
			mill_dia = self.toolcfg.mill_tool().dia
			self.C("toolpath extents:")
			self.C("    X: {:0.3f} - {:0.3f}",
			    self.panel_xstart - mill_dia / 2,
			    self.panel_x2 + mill_dia / 2)
			self.C("    Y: {:0.3f} - {:0.3f}",
			    self.panel_ystart - mill_dia / 2,
			    self.panel_y2 + mill_dia / 2)
		self.C("")
		self.G("M5 G17 G21 G40 G49 G50 G69 G80 G90 G98")
		self.G("G53 G0 Z0")
		self.G("G54")
		self.newblock()

		if self.drills:
			if self.toolcfg.predrill_tool() is not None:
				self.output_spot_drill()
			self.output_drills()
		if self.rect_cutouts:
			self.output_rect_cutouts()
		if self.round_cutouts:
			self.output_round_cutouts()
		if self.cutout_panel:
			self.output_panel_cutout()
		# XXX chamfer?
		self.G("G53 G0 X0 Y0 Z0")
		self.G("G54")
		self.C("FINISH")
	def add_panel_cutout(self):
		"""Prepare operations to cut out the panel"""
		tool = self.toolcfg.mill_tool()
		need = min(self.eurorack_width, self.eurorack_height)
		if tool.dia * 2.0 > need:
			print(("Mill tool #{} dia {:0.3f} too big for "+
			    "required clearance {:0.3f}").format(
			    tool.num, tool.dia, need))
			sys.exit(1)
		try:
			drill_dia = sorted([x for x
			    in self.toolcfg.drill_dias()
			    if x >= tool.dia])[:2][-1]
		except IndexError:
			raise ValueError("No suitable drill found")
		self.panel_xstart = -drill_dia * 0.8
		self.panel_ystart = -drill_dia * 0.8
		drillhit = GCodeDrillHit("panel cutout start", "",
		    drill_dia, self.eurorack_thickness,
		    self.panel_xstart, self.panel_ystart)
		self.add_drill(drillhit)
		# Leave a little horizontal space for module stacking.
		self.panel_x1 = 0.1
		self.panel_x2 = self.eurorack_width - 0.1
		self.panel_y1 = 0
		self.panel_y2 = self.eurorack_height
		# Add mounting hole drills.
		if self.hp <= 1:
			# No mounting holes.
			return
		elif self.hp <= 8:
			# One column of mounting holes
			self.add_drill(GCodeDrillHit("panel mount B", "",
			    self.mount_drill, self.eurorack_thickness,
			    7.5, 3.0))
			self.add_drill(GCodeDrillHit("panel mount T", "",
			    self.mount_drill, self.eurorack_thickness,
			    7.5, self.eurorack_height - 3.0))
		else:
			# Two columns of mounting holes.
			self.add_drill(GCodeDrillHit("panel mount B/L", "",
			    drill_dia, self.eurorack_thickness,
			    7.5, 3.0))
			self.add_drill(GCodeDrillHit("panel mount T/L", "",
			    self.mount_drill, self.eurorack_thickness,
			    7.5, self.eurorack_height - 3.0))
			self.add_drill(GCodeDrillHit("panel mount B/R", "",
			    self.mount_drill, self.eurorack_thickness,
			    self.eurorack_width - 7.5, 3.0))
			self.add_drill(GCodeDrillHit("panel mount T/R", "",
			    self.mount_drill, self.eurorack_thickness,
			    self.eurorack_width - 7.5,
			    self.eurorack_height - 3.0))
	def output_panel_cutout(self):
		"""Emit GCode to cut out the panel"""
		tool = self.toolcfg.mill_tool()
		self.C("START panel cutout")
		self.G("M1")
		self.G("T{} M6   ; Tool {}: {:0.1f}mm endmill",
		    tool.num, tool.num, tool.dia)
		self.G("G43 H{}", tool.num)
		self.G("S{:0.3f} M3", tool.speed)
		depth = self.eurorack_thickness + self.mill_depth_clearance
		self.G("G0 X{:0.3f} Y{:0.3f} Z{:0.3f}  ; entry",
		    self.panel_xstart, self.panel_ystart, self.hover)
		self.coolant_on()
		# Move to start position near bottom left.
		self.G("G1 F{:0.3f} Z{:0.3f}", tool.downfeed, -depth)
		# On cutout above start point, enabling RHS cutter comp.
		self.G("G42 D{} F{} X{:0.3f} Y{:0.3f}", tool.num, tool.feed,
		    self.panel_x1, self.panel_y1)
		# Bottom right
		self.G("X{:0.3f}", self.panel_x2)
		# Top right
		self.G("Y{:0.3f}", self.panel_y2)
		# Top left
		self.G("X{:0.3f}", self.panel_x1)
		# Bottom left
		self.G("Y{:0.3f}", self.panel_y1)
		# A little past start point.
		self.G("X{:0.3f}", min(self.panel_xstart + tool.dia,
		    self.panel_x2))
		self.G("G40")
		self.coolant_off()
		self.G("G0 Z20")
		self.G("M5")
		self.C("DONE panel cutout")
		self.newblock()
	def output_rect_cutouts(self):
		"""Emit GCode to cut out rectangular features"""
		tool = self.toolcfg.mill_tool()
		self.C("START rectangular cutouts")
		self.G("M1")
		self.G("T{} M6   ; Tool {}: {:0.1f}mm endmill",
		    tool.num, tool.num, tool.dia)
		self.G("G43 H{}", tool.num)
		self.G("S{:0.3f} M3", tool.speed)
		for rc in self.rect_cutouts:
			depth = rc.depth + self.mill_depth_clearance
			self.C("BEGIN cutout {} ({})", rc.ref, rc.footprint)
			self.C("EXTENTS X{:0.3f}-X{:0.3f} W{:0.3f} " +
			    "Y{:0.3f}-Y{:0.3f} H{:0.3f}",
			    rc.x1, rc.x2, rc.x2 - rc.x1,
			    rc.y1, rc.y2, rc.y1 - rc.y2)
			# Move to start position near top left.
			self.G("G0 X{:0.3f} Y{:0.3f} Z{:0.3f}  ; entry",
			    rc.xstart, rc.ystart, self.hover)
			self.coolant_on()
			self.G("G1 F{:0.3f} Z{:0.3f}", tool.downfeed, -depth)
			# Top edge 1xdia off left, enabling RHS cutter comp.
			self.G("G42 D{} F{} X{:0.3f} Y{:0.3f}", tool.num,
			    tool.feed, rc.x1 + tool.dia, rc.y1);
			# Top right
			self.G("X{:0.3f}", rc.x2)
			# Bottom right
			self.G("Y{:0.3f}", rc.y2)
			# Bottom left
			self.G("X{:0.3f}", rc.x1)
			# Top left
			self.G("Y{:0.3f}", rc.y1)
			# A little past start point.
			self.G("X{:0.3f}", min(rc.xstart + tool.dia, rc.x2))
			self.G("G40")
			self.coolant_off()
			self.G("G0 Z{:0.3f}", self.hover)
			self.C("END {}", rc.ref)
		self.G("G0 Z20")
		self.G("M5")
		self.C("DONE rectangular cutouts")
		self.newblock()
	def output_round_cutouts(self):
		"""Emit GCode to cut out round features"""
		tool = self.toolcfg.mill_tool()
		self.C("START round cutouts")
		self.G("M1")
		self.G("T{} M6   ; Tool {}: {:0.1f}mm endmill",
		    tool.num, tool.num, tool.dia)
		self.G("G43 H{}", tool.num)
		self.G("S{:0.3f} M3", tool.speed)
		for rc in self.round_cutouts:
			depth = rc.depth + self.mill_depth_clearance
			self.C("BEGIN cutout {} ({})", rc.ref, rc.footprint)
			self.C("EXTENTS X{:0.3f} Y{:0.3f} D{:0.3f}",
			    rc.xpos, rc.ypos, rc.dia)
			# Move to recorded start position
			self.G("G0 X{:0.3f} Y{:0.3f} Z{:0.3f}  ; entry",
			    rc.xstart, rc.ystart, self.hover)
			self.coolant_on()
			self.G("G1 F{:0.3f} Z{:0.3f}", tool.downfeed, -depth)
			# Enabling RHS cutter comp and move to left edge.
			self.G("G42 D{} F{} X{:0.3f}", tool.num,
			    tool.feed, rc.xpos - (rc.dia/2), rc.ypos);
			# Circle
			self.G("G17 G02 F{} I{}", tool.feed, rc.dia/2)
			self.G("G40")
			self.coolant_off()
			self.G("G0 Z{:0.3f}", self.hover)
			self.C("END {}", rc.ref)
		self.G("G0 Z20")
		self.G("M5")
		self.C("DONE round cutouts")
		self.newblock()
	def drill_point_depth(self, dia, theta=120.0):
		"""Calculate the length of a drill point"""
		return (dia / 2.0) * math.tan(math.radians(90.0-(theta/2.0)))
	def emit_drillhits(self, tool, drillhits, descr):
		"""Emit GCode to drill holes of a particular size"""
		first = True
		for drill in drillhits:
			depth = (drill.depth +
			    self.drill_point_depth(tool.dia) +
			    self.drill_depth_clearance)
			peck = drill.dia / 4
			if first:
				first = False
				self.C("START {}", descr)
				self.G("M1")
				self.G("T{} M6   ; Tool {}: {:0.1f}mm drill",
				    tool.num, tool.num, tool.dia)
				self.G("G43 H{}", tool.num)
				self.G("G0 X{:0.3f} Y{:0.3f} Z{:0.3f}  ; {}",
				    drill.xpos, drill.ypos,
				    self.hover, drill.ref)
				self.G("G1 F{:0.3f}", tool.feed)
				self.G("S{:0.3f} M3", tool.speed)
				self.G("G98 G73 R{:0.3f} Q{:0.3f}",
				    self.hover, peck)
				self.coolant_on()
			self.G("X{:0.3f} Y{:0.3f} Z{:0.3f}  ; {}",
			    drill.xpos, drill.ypos, -depth, drill.ref)
		self.coolant_off()
		self.G("G0 Z20")
		self.G("G80 M5")
		self.C("DONE {}", descr)
	def output_spot_drill(self):
		"""Emit GCode for spot drilling larger holes"""
		tool = self.toolcfg.predrill_tool()
		drillhits = [x for v in self.drills.values()
			for x in v if x.dia >= tool.dia]
		drillhits.sort(key=operator.attrgetter("xpos", "ypos"))
		descr = "Spot drill holes over {:0.1f}mm".format(tool.dia)
		self.emit_drillhits(tool, drillhits, descr)
		# XXX option to use spotting drill instead of regular drill
		# XXX toolcfg option for minimum size for spot drill
		self.newblock()
	def output_drills(self):
		"""Emit GCode for drilling holes"""
		# Drill smallest to largest.
		for dia in sorted(self.drills.keys()):
			if self.toolcfg.predrill_tool() is not None and \
			   self.toolcfg.predrill_tool().dia == dia:
				continue
			tool = self.toolcfg.drill_by_dia(dia)
			descr = "Drill {:0.1f}mm holes".format(tool.dia)
			drillhits = sorted(self.drills[dia],
			    key=operator.attrgetter("xpos", "ypos"))
			self.emit_drillhits(tool, drillhits, descr)
			self.newblock()

def output_gcode(args, panelbrd):
	toolcfg = None
	try:
		toolcfg = GCodeToolTable(args.gcode_tool_config)
	except FileNotFoundError:
		pass
	if toolcfg is None:
		try:
			toolcfg = GCodeToolTable(os.path.join(sys.path[0],
			    args.gcode_tool_config))
		except (IndexError, FileNotFoundError):
			pass
	if toolcfg is None:
		print("Cannot find tool configuration {}".format(
		    args.gcode_tool_config), file=sys.stderr)
		sys.exit(1)

	gcode_outputter = GCodeOutput(panelbrd, toolcfg,
	    args.gcode_cutout_panel, args.gcode_mount_drill)
	gcode_outputter.output()
