"""Build native FreeCAD panels in FreeCAD's own Python interpreter."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile


def write_model(job, filename, command=None):
    if not filename.lower().endswith('.fcstd'):
        raise RuntimeError('FreeCAD output filename must end in .FCStd')
    if command is None:
        command = shutil.which('FreeCADCmd') or shutil.which('freecadcmd')
    if command is None:
        raise RuntimeError('FreeCADCmd/freecadcmd is required for FreeCAD output; '
                           'install FreeCAD or specify --freecad_command')
    filename = os.path.abspath(filename)
    # Generate beside the destination so publishing the completed file is an
    # atomic rename, and a failed FreeCAD run leaves existing output intact.
    with tempfile.TemporaryDirectory(prefix='.boardcam-freecad-',
                                     dir=os.path.dirname(filename)) as workdir:
        output = os.path.join(workdir, 'panel.FCStd')
        job = dict(job, output=output)
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
        ET.SubElement(visibility, 'Bool', value='true' if obj == panel else 'false')

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

    doc = App.newDocument('EurorackPanel')
    doc.Label = 'Eurorack panel ({} HP)'.format(job['hp'])
    panel = doc.addObject('Part::Box', 'Blank')
    panel.Label = 'Panel blank (2 mm)'
    panel.Length = job['width']
    panel.Width = job['height']
    panel.Height = job['thickness']

    cutouts = []
    # Match the mounting-hole layout used by the G-code panel mode.
    columns = []
    if job['hp'] > 1:
        columns.append(7.5)
    if job['hp'] > 8:
        columns.append(job['width'] - 7.5)
    for column, x in enumerate(columns, 1):
        for row, y in enumerate((3.0, job['height'] - 3.0), 1):
            cutouts.append(dict(name='Mount_{}_{}'.format(column, row),
                                shape='circle', diameter=3.2, x=x, y=y))
    cutouts.extend(job['cutouts'])

    for index, cutout in enumerate(cutouts, 1):
        # Extend cutters beyond both faces to ensure through holes.
        if cutout['shape'] == 'circle':
            tool = doc.addObject('Part::Cylinder', 'Hole{}'.format(index))
            tool.Radius = cutout['diameter'] / 2
        else:
            tool = doc.addObject('Part::Box', 'Rectangle{}'.format(index))
            tool.Length = cutout['width']
            tool.Width = cutout['height']
        tool.Label = cutout['name'] + ' cutter'
        tool.Height = job['thickness'] + 2
        tool.Placement = App.Placement(
            App.Vector(cutout['x'], cutout['y'], -1),
            App.Rotation(App.Vector(0, 0, 1), cutout.get('angle', 0)))
        result = doc.addObject('Part::Cut', 'Cut{}'.format(index))
        result.Label = cutout['name'] + ' cutout'
        result.Base = panel
        result.Tool = tool
        panel.Visibility = False
        tool.Visibility = False
        panel = result

    panel.Label = 'Front panel'
    panel.Visibility = True
    doc.recompute()
    if (panel.Shape.isNull() or not panel.Shape.isValid() or
            len(panel.Shape.Solids) != 1):
        raise ValueError('Cutouts did not produce a valid single panel solid')
    doc.saveAs(job['output'])
    save_view_state(doc, panel, job['output'])
    App.closeDocument(doc.Name)


if __name__ == '__main__':
    with open(sys.argv[-1]) as stream:
        build_panel(json.load(stream))
