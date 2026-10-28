"""Build FreeCAD panels and STL meshes in FreeCAD's own Python interpreter."""

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile


def write_model(job, filename, command=None, output_format='freecad'):
    extensions = {'freecad': '.FCStd', 'stl': '.stl'}
    if output_format not in extensions:
        raise RuntimeError('Unsupported model format: ' + output_format)
    extension = extensions[output_format]
    if not filename.lower().endswith(extension.lower()):
        raise RuntimeError('Output filename must end in ' + extension)
    if command is None:
        command = shutil.which('FreeCADCmd') or shutil.which('freecadcmd')
    if command is None:
        raise RuntimeError('FreeCADCmd/freecadcmd is required for model output; '
                           'install FreeCAD or specify --freecad_command')
    filename = os.path.abspath(filename)
    # Generate beside the destination so publishing the completed file is an
    # atomic rename, and a failed FreeCAD run leaves existing output intact.
    with tempfile.TemporaryDirectory(prefix='.boardcam-freecad-',
                                     dir=os.path.dirname(filename)) as workdir:
        output = os.path.join(workdir, 'panel' + extension)
        job = dict(job, output=output, output_format=output_format)
        job_path = os.path.join(workdir, 'panel.json')
        with open(job_path, 'w') as stream:
            json.dump(job, stream)
        # FreeCAD executes .py files as imported modules, so use runpy to
        # invoke the worker's normal __main__ entry point.
        script_path = os.path.join(workdir, 'generate.py')
        with open(script_path, 'w') as stream:
            stream.write('import runpy\nrunpy.run_path({!r}, run_name="__main__")\n'
                         .format(os.path.abspath(__file__)))
        result = subprocess.run(
            [command, '--user-cfg', os.path.join(workdir, 'user.cfg'),
             '--system-cfg', os.path.join(workdir, 'system.cfg'),
             script_path, '--pass', job_path],
            stdin=subprocess.DEVNULL, capture_output=True, text=True)
        # FreeCAD can report script exceptions without a failing exit status.
        if result.returncode != 0 or not os.path.isfile(output):
            raise RuntimeError('FreeCAD model generation failed:\n' +
                               result.stdout + result.stderr)
        os.replace(output, filename)


def save_view_state(doc, panel, filename):
    """Persist visibility and a fitted top view without starting the GUI.

    FreeCADCmd saves no GuiDocument.xml, and the GUI may discard the App
    object's Visibility when restoring a file without view-provider data.
    Use the same GUI metadata format as FreeCAD's OfflineRenderingUtils.
    """
    root = ET.Element('Document', SchemaVersion='1')
    providers = ET.SubElement(root, 'ViewProviderData', Count=str(len(doc.Objects)))
    for obj in doc.Objects:
        provider = ET.SubElement(providers, 'ViewProvider', name=obj.Name, expanded='0')
        properties = ET.SubElement(provider, 'Properties', Count='1')
        visibility = ET.SubElement(properties, 'Property', name='Visibility',
                                   type='App::PropertyBool')
        visible = obj == panel or obj == panel.getParentGeoFeatureGroup()
        ET.SubElement(visibility, 'Bool', value='true' if visible else 'false')

    bounds = panel.Shape.BoundBox
    center = bounds.Center
    span = max(bounds.XLength, bounds.YLength, bounds.ZLength)
    distance = 2 * span
    # A square camera envelope covers the panel with 10% margin. Coin's
    # ADJUST_CAMERA expands the view to accommodate other viewport aspects.
    camera = ('OrthographicCamera {{ viewportMapping ADJUST_CAMERA '
              'position {x} {y} {z} orientation 0 0 1 0 '
              'nearDistance {near} farDistance {far} aspectRatio 1 '
              'focalDistance {distance} height {height} }}').format(
                  x=center.x, y=center.y, z=center.z + distance,
                  near=span, far=3 * span, distance=distance, height=1.1 * span)
    ET.SubElement(root, 'Camera', settings=camera)
    with zipfile.ZipFile(filename, 'a', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('GuiDocument.xml',
                         ET.tostring(root, encoding='utf-8', xml_declaration=True))


def build_panel(job):
    import FreeCAD as App
    import Part
    import Sketcher

    doc = App.newDocument('EurorackPanel')
    doc.Label = 'Eurorack panel ({} HP)'.format(job['hp'])
    body = doc.addObject('PartDesign::Body', 'Body')
    body.Label = 'Panel body'
    sketch = body.newObject('Sketcher::SketchObject', 'Sketch')
    sketch.Label = 'Panel outline and cutouts'

    def dimension(constraint, name):
        index = sketch.addConstraint(constraint)
        sketch.renameConstraint(index, name)

    def locate(geometry, point, reference, x, y, name):
        """Constrain a point to signed offsets from another sketch point."""
        ref_geometry, ref_point = reference
        if abs(x) < 1e-9 and abs(y) < 1e-9:
            sketch.addConstraint(Sketcher.Constraint(
                'Coincident', ref_geometry, ref_point, geometry, point))
            return
        for axis, value in [('X', x), ('Y', y)]:
            if abs(value) < 1e-9:
                alignment = 'Vertical' if axis == 'X' else 'Horizontal'
                sketch.addConstraint(Sketcher.Constraint(
                    alignment, ref_geometry, ref_point, geometry, point))
            else:
                dimension(Sketcher.Constraint('Distance' + axis,
                          ref_geometry, ref_point, geometry, point, value),
                          name + axis)

    def rectangle(x, y, width, height, angle, name, reference, dx, dy,
                  construction=False):
        theta = math.radians(angle)
        vertices = [App.Vector(x + u * math.cos(theta) - v * math.sin(theta),
                               y + u * math.sin(theta) + v * math.cos(theta), 0)
                    for u, v in [(0, 0), (width, 0), (width, height), (0, height)]]
        edges = sketch.addGeometry([Part.LineSegment(vertices[i], vertices[(i + 1) % 4])
                                    for i in range(4)], construction)
        constraints = [Sketcher.Constraint('Coincident', edges[i], 2,
                                          edges[(i + 1) % 4], 1)
                       for i in range(4)]
        if abs(angle / 90 - round(angle / 90)) < 1e-9:
            first_horizontal = round(angle / 90) % 2 == 0
            for i, edge in enumerate(edges):
                horizontal = first_horizontal == (i % 2 == 0)
                constraints.append(Sketcher.Constraint(
                    'Horizontal' if horizontal else 'Vertical', edge))
        else:
            constraints.extend([
                Sketcher.Constraint('Perpendicular', edges[0], edges[1]),
                Sketcher.Constraint('Parallel', edges[0], edges[2]),
                Sketcher.Constraint('Parallel', edges[1], edges[3])])
        sketch.addConstraint(constraints)
        if abs(angle / 90 - round(angle / 90)) >= 1e-9:
            dimension(Sketcher.Constraint('Angle', edges[0], theta % (2 * math.pi)),
                      name + 'Angle')
        dimension(Sketcher.Constraint('Distance', edges[0], width), name + 'Width')
        dimension(Sketcher.Constraint('Distance', edges[1], height), name + 'Height')
        locate(edges[0], 1, reference, dx, dy, name + 'Offset')
        return edges[0]

    outline = rectangle(0, 0, job['width'], job['height'], 0, 'Panel',
                        (-1, 1), 0, 0)
    # Construction geometry supplies a PCB datum without contributing any
    # edges to the pad. Component positions are measured from its lower left.
    board = job.get('board', dict(x=0, y=0, width=job['width'], height=job['height']))
    board_edge = rectangle(board['x'], board['y'], board['width'], board['height'],
                           0, 'Board', (outline, 1), board['x'], board['y'], True)

    cutouts = []
    # Match the mounting-hole layout used by the G-code panel mode.
    columns = []
    if job['hp'] > 1:
        columns.append(7.5)
    if job['hp'] > 8:
        columns.append(job['width'] - 7.5)
    for column, x in enumerate(columns, 1):
        for row, y in enumerate((3.0, job['height'] - 3.0), 1):
            # Mounting holes follow the corresponding panel corner when
            # panel dimensions change; component holes follow the PCB datum.
            corner = (outline if column == 1 else outline + 1, 1 if row == 1 else 2)
            if row == 2 and column == 1:
                corner = (outline + 3, 1)
            cutouts.append(dict(name='Mount_{}_{}'.format(column, row),
                                shape='circle', diameter=3.2, x=x, y=y,
                                reference=corner,
                                dx=7.5 if column == 1 else -7.5,
                                dy=3.0 if row == 1 else -3.0))
    cutouts.extend(job['cutouts'])

    for cutout in cutouts:
        reference = cutout.get('reference', (board_edge, 1))
        dx = cutout.get('dx', cutout['x'] - board['x'])
        dy = cutout.get('dy', cutout['y'] - board['y'])
        name = cutout['name']
        if cutout['shape'] == 'circle':
            circle = sketch.addGeometry(Part.Circle(
                App.Vector(cutout['x'], cutout['y'], 0), App.Vector(0, 0, 1),
                cutout['diameter'] / 2), False)
            dimension(Sketcher.Constraint('Diameter', circle, cutout['diameter']),
                      name + 'Diameter')
            locate(circle, 3, reference, dx, dy, name + 'Offset')
        else:
            rectangle(cutout['x'], cutout['y'], cutout['width'], cutout['height'],
                      cutout.get('angle', 0), name, reference, dx, dy)

    if sketch.solve() != 0 or not sketch.FullyConstrained:
        raise ValueError('Panel sketch could not be fully constrained')
    panel = body.newObject('PartDesign::Pad', 'Pad')
    panel.Profile = sketch
    panel.Length = job['thickness']
    body.Tip = panel
    panel.Label = 'Front panel'
    doc.recompute()
    for obj in doc.Objects:
        obj.Visibility = obj == body or obj == panel
    if (panel.Shape.isNull() or not panel.Shape.isValid() or
            len(panel.Shape.Solids) != 1):
        raise ValueError('Cutouts did not produce a valid single panel solid')
    bounds = panel.Shape.BoundBox
    expected = (0, 0, 0, job['width'], job['height'], job['thickness'])
    actual = (bounds.XMin, bounds.YMin, bounds.ZMin,
              bounds.XMax, bounds.YMax, bounds.ZMax)
    if any(abs(a - b) > 1e-6 for a, b in zip(actual, expected)):
        raise ValueError('Cutouts extend beyond the panel outline')
    if job.get('output_format', 'freecad') == 'stl':
        import MeshPart
        # Use absolute tolerances independent of FreeCAD's user preferences.
        mesh = MeshPart.meshFromShape(Shape=panel.Shape, LinearDeflection=0.01,
                                      AngularDeflection=math.radians(5), Relative=False)
        if not mesh.isSolid():
            raise ValueError('Panel did not produce a closed STL mesh')
        mesh.write(job['output'])
    else:
        doc.saveAs(job['output'])
        save_view_state(doc, panel, job['output'])
    App.closeDocument(doc.Name)


if __name__ == '__main__':
    with open(sys.argv[-1]) as stream:
        build_panel(json.load(stream))
