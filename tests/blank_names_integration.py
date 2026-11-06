"""Blank names must suppress object labels for all roles and grouping modes."""
from pathlib import Path
import os, tempfile

ROOT = Path(__file__).resolve().parents[1]
exec((ROOT / 'tests/measurement_integration.py').read_text(encoding='utf-8').split('from RingyoZumenMaker.main import Main')[0])
from RingyoZumenMaker.processing_provider import DrawingProvider, ALGORITHM_ID, ROLES, parameters_from_dock
from RingyoZumenMaker.drawing_service import DrawingService, request_from_dock
from RingyoZumenMaker.main import Main
from processing.gui.BatchAlgorithmDialog import BatchAlgorithmDialog

project = QgsProject.instance()
layer = QgsVectorLayer('Point?crs=EPSG:6678', '測点', 'memory')
layer.dataProvider().addAttributes([QgsField('number', QVariant.Int), QgsField('section', QVariant.String)])
layer.updateFields()
for section, coords in [('A', [(0,0),(100,0),(100,100),(0,100)]),
                        ('B', [(200,0),(300,0),(300,100),(200,100)])]:
    for number, (x,y) in enumerate(coords):
        f = QgsFeature(layer.fields())
        f.setAttributes([number, section])
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x,y)))
        layer.dataProvider().addFeature(f)
layer.updateExtents()
project.addMapLayer(layer)
provider = DrawingProvider()
QgsApplication.processingRegistry().addProvider(provider)
algorithm = QgsApplication.processingRegistry().algorithmById(ALGORITHM_ID)
context = QgsProcessingContext()
context.setProject(project)
root = Path(tempfile.mkdtemp(prefix='blank-names-', dir=QA_ROOT))
print('OUTPUT', root, flush=True)
params = {'CRS': 'EPSG:6678', 'OUTPUT': str(root), 'CREATE_PROJECT': False, 'SCALE': 3000}
for prefix, role, label in ROLES:
    params.update({prefix: layer, prefix+'_GROUP': '"section"', prefix+'_ORDER': '"number"',
                   prefix+'_LABEL': '"number"', prefix+'_NAME': ''})

request = algorithm.request(params, context)
items = request.config['polygons']['items'] + request.config['lines']['items']
assert len(items) == 8
assert all(item['name'] == '' for item in items), [item['name'] for item in items]
service = DrawingService(request)
result = service.run()
for page in service.shuis + service.haisuis:
    assert page.name_edit.text() == ''
    assert not page.line_layer.labelsEnabled(), page.role
    assert page.pt_layer.labelsEnabled(), 'Measurement labels must remain enabled'
assert Path(result['HTML']).is_file()
assert all(name == '' for name in [item['name'] for item in service.configuration_data()['polygons']['items']])
print('ALL FOUR ROLES: EMPTY NAMES, NO OBJECT LABELS, MEASUREMENT LABELS RETAINED OK', flush=True)

# A cleared optional batch widget may return either an empty string or NULL.
dialog = BatchAlgorithmDialog(algorithm.create())
panel = dialog.mainWidget()
for name, value in params.items():
    panel.tblParameters.cellWidget(0, panel.parameter_to_column[name]).setRowValue(0, value, context)
batch_params, ok = panel.parametersForRow(0, context, warnOnInvalid=True)
assert ok
batch_request = algorithm.request(batch_params, context)
assert all(item['name'] == '' for group in ('polygons', 'lines') for item in batch_request.config[group]['items'])
print('ACTUAL EMPTY BATCH CELLS DO NOT FALL BACK TO ROLE NAMES OK', flush=True)

dock = Main()
dock.crs.setCrs(QgsCoordinateReferenceSystem('EPSG:6678'))
dock.scale.setScale(3000)
dock.fileName.setFilePath(str(root))
dock.outputMode.setCurrentIndex(1)
for prefix, role, label in ROLES:
    page = dock.add_role_page(role)
    page.name_edit.clear()
    page.point_layer.setLayer(layer)
    page.sort_exp.setExpression('"number"')
    page.sokuten_labels.set_expressions(['"number"'], 0)
    if role in ('land', 'ex_polygon'):
        page.object_exp.setExpression('"section"')
    else:
        page.filter_exp.setExpression('"section" = \'A\'')
gui_service = DrawingService(request_from_dock(dock))
assert len(gui_service.role_pages['land']) == 2
assert len(gui_service.role_pages['ex_polygon']) == 2
assert all(p.name_edit.text() == '' for p in gui_service.shuis + gui_service.haisuis)
copied_request = algorithm.request(parameters_from_dock(dock), context)
assert all(item['name'] == '' for group in ('polygons', 'lines') for item in copied_request.config[group]['items'])
print('GUI GROUPING AND COPIED PROCESSING PARAMETERS KEEP EMPTY NAMES OK', flush=True)

# Unspecified names still use the normal initial defaults, and explicitly set
# names continue to receive short group suffixes.
default_params = {key: value for key, value in params.items() if not key.endswith('_NAME')}
default_request = algorithm.request(default_params, context)
assert default_request.config['polygons']['items'][0]['name'] == '施行地1'
for prefix, role, label in ROLES:
    params[prefix+'_NAME'] = '   '
assert all(item['name'] == '' for group in ('polygons', 'lines')
           for item in algorithm.request(params, context).config[group]['items'])
print('WHITESPACE NAMES HIDDEN AND OMITTED-PARAMETER DEFAULTS RETAINED OK', flush=True)
dock.disconnect_project_signals()

# Clearing a formerly named object must also suppress the label in a retained
# QGIS style/theme when regenerating an existing editable project.
named_params = dict(default_params, OUTPUT=str(root / 'regenerate'), CREATE_PROJECT=True)
named_service = DrawingService(algorithm.request(named_params, context))
named_result = named_service.run()
assert project.read(named_result['PROJECT'])
from RingyoZumenMaker.unified_project import metadata
from RingyoZumenMaker.drawing_inputs import DrawingRequest
saved_config = metadata(project)['config']
saved_config['output']['directory'] = str(root / 'regenerate')
saved_layers = {}
for group in ('polygons', 'lines'):
    for item in saved_config[group]['items']:
        item['name'] = ''
        item['name_property'] = {}
        saved_layers[item['drawing_uid']] = project.mapLayer(item['layer']['id'])
blank_service = DrawingService(DrawingRequest(saved_config, saved_layers), preserve_active_project=True)
blank_service.run()
info = metadata(project)
for drawing in info['layouts']:
    style_name = '図面:' + drawing['key']
    for key, layer_id in info['datasets'].items():
        if key.startswith('output:') and key.endswith(':line_layer'):
            saved_line = project.mapLayer(layer_id)
            if style_name in saved_line.styleManager().styles():
                saved_line.styleManager().setCurrentStyle(style_name)
                assert not saved_line.labelsEnabled(), key
print('CLEARING NAMES ON REGENERATION DISABLES OLD THEME LABELS OK', flush=True)
print('PASS', flush=True)
os._exit(0)
