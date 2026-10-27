"""End-to-end geometry checks; require KiCad and FreeCADCmd/freecadcmd."""

import contextlib
import io
import json
import math
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
import xml.etree.ElementTree as ET
import zipfile

import pcbnew

import board_cam
import freecad_panel


FREECAD = shutil.which('FreeCADCmd') or shutil.which('freecadcmd')


@unittest.skipUnless(FREECAD, 'FreeCAD command-line executable not installed')
class FreeCADPanelTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.workdir = Path(self.directory.name)

    def inspect_model(self, filename, probes):
        report = self.workdir / 'report.json'
        script = self.workdir / 'inspect_panel.py'
        script.write_text('''import json
import FreeCAD as App
doc = App.openDocument({filename!r})
panel = doc.Objects[-1]
shape = panel.Shape
bounds = shape.BoundBox
data = dict(valid=shape.isValid(), solids=len(shape.Solids),
            size=[bounds.XLength, bounds.YLength, bounds.ZLength],
            volume=shape.Volume,
            visible=[obj.Label for obj in doc.Objects if obj.Visibility],
            probes=[shape.isInside(App.Vector(*p), 1e-7, True)
                    for p in {probes!r}])
with open({report!r}, 'w') as stream:
    json.dump(data, stream)
App.closeDocument(doc.Name)
'''.format(filename=str(filename), probes=probes, report=str(report)))
        result = subprocess.run(
            [FREECAD, '--user-cfg', str(self.workdir / 'user.cfg'),
             '--system-cfg', str(self.workdir / 'system.cfg'), str(script)],
            capture_output=True, text=True, stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(report.exists(), result.stdout + result.stderr)
        return json.loads(report.read_text())

    def test_panel_dimensions_cutouts_rotation_and_visibility(self):
        board = pcbnew.BOARD()

        def point(x, y):
            return pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y))

        edge = pcbnew.PCB_SHAPE(board)
        edge.SetShape(pcbnew.SHAPE_T_RECT)
        edge.SetLayer(pcbnew.Edge_Cuts)
        edge.SetStart(point(10, 20))
        edge.SetEnd(point(100, 120))
        board.Add(edge)
        board_cam.PanelComponent.add_known(board_cam.ComponentParams(
            'boardcam-tests:freecad-circle', hole_dia=6.6))
        board_cam.PanelComponent.add_known(board_cam.ComponentParams(
            'boardcam-tests:freecad-rectangle', xoffset=1, yoffset=2,
            rect_width=6, rect_height=4))

        def footprint(name, reference, x, y, angle):
            obj = pcbnew.FOOTPRINT(board)
            obj.SetFPID(pcbnew.LIB_ID('boardcam-tests', name))
            obj.SetReference(reference)
            obj.SetPosition(point(x, y))
            obj.SetOrientationDegrees(angle)
            board.Add(obj)

        footprint('freecad-circle', 'J1', 20, 40, 0)
        # Centered PCB: X offset 0.72, Y offset 14.25. FreeCAD Y runs up.
        probes = [(10.72, 94.25, 1), (7.5, 3, 1), (83.94, 125.5, 1),
                  (1, 1, 1)]
        expected = [False, False, False, True]
        rectangles = [(40, 45, 0), (60, 45, 90), (80, 45, 180),
                      (40, 85, 270), (70, 85, 37)]
        for index, (x, y, angle) in enumerate(rectangles):
            footprint('freecad-rectangle', 'R{}'.format(index), x, y, angle)
            # Independent point probes: rectangle center and just outside
            # the left edge, including its footprint-local (1, 2) offset.
            theta = math.radians(angle)
            for local_x, local_y, inside in [(4, 4, False), (0.8, 4, True)]:
                dx = local_x * math.cos(theta) + local_y * math.sin(theta)
                dy = local_y * math.cos(theta) - local_x * math.sin(theta)
                probes.append((x - 10 + 0.72 + dx,
                               128.5 - (y - 20 + 14.25 + dy), 1))
                expected.append(inside)

        output = self.workdir / 'panel with spaces.FCStd'
        args = SimpleNamespace(freecad_output=str(output), freecad_command=FREECAD)
        with contextlib.redirect_stderr(io.StringIO()):
            panel = board_cam.PanelBoard('synthetic.kicad_pcb', board)
            board_cam.output_eurorack_freecad(args, panel)
        report = self.inspect_model(output, probes)
        self.assertTrue(report['valid'])
        self.assertEqual(report['solids'], 1)
        for actual, wanted in zip(report['size'], (91.44, 128.5, 2)):
            self.assertAlmostEqual(actual, wanted)
        volume = (91.44 * 128.5 - math.pi * 3.3 ** 2 -
                  4 * math.pi * 1.6 ** 2 - 5 * 6 * 4) * 2
        self.assertAlmostEqual(report['volume'], volume, places=5)
        self.assertEqual(report['probes'], expected)
        self.assertEqual(report['visible'], ['Front panel'])
        # App.Visibility alone does not establish visibility when opened in
        # the GUI. Check the saved view-provider flags and actual Coin camera.
        with zipfile.ZipFile(output) as archive:
            gui = ET.fromstring(archive.read('GuiDocument.xml'))
        providers = gui.findall('./ViewProviderData/ViewProvider')
        visible = [provider.attrib['name'] for provider in providers
                   if provider.find('./Properties/Property[@name="Visibility"]/Bool')
                   .attrib['value'] == 'true']
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0], providers[-1].attrib['name'])
        self.assertTrue(visible[0].startswith('Cut'))
        from pivy import coin
        camera_input = coin.SoInput()
        camera_input.setBuffer(gui.find('Camera').attrib['settings'])
        camera = coin.SoDB.readAll(camera_input).getChild(0)
        self.assertIsInstance(camera, coin.SoOrthographicCamera)
        position = camera.position.getValue()
        self.assertAlmostEqual(position[0], 91.44 / 2, places=5)
        self.assertAlmostEqual(position[1], 128.5 / 2, places=5)
        self.assertGreater(camera.height.getValue(), 128.5)
        # All corners must be visible in landscape, square and portrait views.
        for aspect in (0.5, 1, 2):
            viewport = coin.SbViewportRegion(int(600 * aspect), 600)
            volume = coin.SoCamera.getViewVolume(camera, viewport,
                                                 coin.SbViewportRegion())
            for x in (0, 91.44):
                for y in (0, 128.5):
                    for z in (0, 2):
                        screen = volume.projectToScreen(coin.SbVec3f(x, y, z))
                        for value in screen:
                            self.assertGreaterEqual(value, 0)
                            self.assertLessEqual(value, 1)

    def test_narrow_panel_mounting_holes(self):
        for hp, count in [(1, 0), (4, 2)]:
            with self.subTest(hp=hp):
                output = self.workdir / 'narrow.FCStd'
                freecad_panel.write_model(dict(width=hp * 5.08, height=128.5,
                                               thickness=2, hp=hp, cutouts=[]),
                                          str(output), FREECAD)
                report = self.inspect_model(output, [])
                expected = (hp * 5.08 * 128.5 - count * math.pi * 1.6 ** 2) * 2
                self.assertAlmostEqual(report['volume'], expected, places=5)

    def test_failed_generation_preserves_existing_output(self):
        output = self.workdir / 'existing.FCStd'
        output.write_bytes(b'existing output')
        job = dict(width=20.32, height=128.5, thickness=2, hp=4,
                   cutouts=[dict(name='Oversized', shape='rectangle',
                                 width=1000, height=1000, x=-100, y=-100)])
        with self.assertRaisesRegex(RuntimeError, 'generation failed'):
            freecad_panel.write_model(job, str(output), FREECAD)
        self.assertEqual(output.read_bytes(), b'existing output')


if __name__ == '__main__':
    unittest.main()
