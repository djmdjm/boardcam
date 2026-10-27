"""Geometry regression tests using the installed KiCad Python bindings.

Run with /usr/bin/python3 -m unittest -v.
"""

import contextlib
import io
import unittest

import pcbnew

import board_cam
import gcode


def point(x, y):
    return pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y))


class PanelGeometryTests(unittest.TestCase):
    def setUp(self):
        self.board = pcbnew.BOARD()
        board_cam.PanelComponent.add_known(board_cam.ComponentParams(
            'boardcam-tests:offset', xoffset=2, yoffset=3, hole_dia=6.6))
        footprint = pcbnew.FOOTPRINT(self.board)
        footprint.SetFPID(pcbnew.LIB_ID('boardcam-tests', 'offset'))
        footprint.SetReference('J1')
        footprint.SetPosition(point(20, 30))
        footprint.SetOrientationDegrees(90)
        self.board.Add(footprint)

    def add_edge(self, shape, start, end):
        edge = pcbnew.PCB_SHAPE(self.board)
        edge.SetShape(shape)
        edge.SetLayer(pcbnew.Edge_Cuts)
        edge.SetStart(point(*start))
        edge.SetEnd(point(*end))
        self.board.Add(edge)

    def panel(self):
        with contextlib.redirect_stderr(io.StringIO()):
            return board_cam.PanelBoard('test.kicad_pcb', self.board)

    def assert_panel_geometry(self, panel):
        self.assertEqual((panel.board_width, panel.board_height), (20, 30))
        self.assertEqual(len(panel.components), 1)
        component = panel.components[0]
        self.assertEqual(component.orient, 90)
        self.assertAlmostEqual(component.hole_x, 13)
        self.assertAlmostEqual(component.hole_y, 8)

    def test_rectangle_outline_and_rotated_offset(self):
        self.add_edge(pcbnew.SHAPE_T_RECT, (10, 20), (30, 50))
        self.assert_panel_geometry(self.panel())

    def test_segments_with_mixed_directions_and_separate_outline(self):
        # Two edges start at the first point: traversal must follow both.
        for start, end in [((10, 20), (30, 20)),
                           ((30, 50), (30, 20)),
                           ((30, 50), (10, 50)),
                           ((10, 20), (10, 50))]:
            self.add_edge(pcbnew.SHAPE_T_SEGMENT, start, end)
        self.add_edge(pcbnew.SHAPE_T_RECT, (100, 100), (110, 110))
        panel = self.panel()
        self.assert_panel_geometry(panel)
        self.assertEqual(len(panel.find_edge_polys(self.board)), 2)

    def test_polygon_outline(self):
        polygon = pcbnew.SHAPE_POLY_SET()
        polygon.NewOutline()
        for x, y in [(10, 20), (30, 20), (30, 50), (10, 50)]:
            polygon.Append(pcbnew.FromMM(x), pcbnew.FromMM(y))
        edge = pcbnew.PCB_SHAPE(self.board)
        edge.SetShape(pcbnew.SHAPE_T_POLY)
        edge.SetLayer(pcbnew.Edge_Cuts)
        edge.SetPolyShape(polygon)
        self.board.Add(edge)
        self.assert_panel_geometry(self.panel())

    def test_gcode_dimensions_and_fallback_to_component_bounds(self):
        self.add_edge(pcbnew.SHAPE_T_RECT, (10, 20), (30, 50))
        panel = self.panel()
        tools = gcode.GCodeToolTable('tools.cfg')
        with contextlib.redirect_stderr(io.StringIO()):
            output = gcode.GCodeOutput(panel, tools)
        self.assertEqual((output.width, output.height), (20, 30))
        panel.board_bounds = None
        with contextlib.redirect_stderr(io.StringIO()):
            output = gcode.GCodeOutput(panel, tools)
        self.assertEqual(output.width, pcbnew.ToMM(panel.bounds.GetWidth()))
        self.assertEqual(output.height, pcbnew.ToMM(panel.bounds.GetHeight()))


if __name__ == '__main__':
    unittest.main()
