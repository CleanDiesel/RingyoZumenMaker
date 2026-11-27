"""Verify optional folder names from literals, attributes and native autofill."""
from pathlib import Path
import os, tempfile, json, copy, importlib

ROOT = Path(__file__).resolve().parents[1]
exec((ROOT / 'tests/measurement_integration.py').read_text(encoding='utf-8').split('from RingyoZumenMaker.main import Main')[0])
from RingyoZumenMaker.main import Main
from RingyoZumenMaker.processing_provider import DrawingProvider, ALGORITHM_ID, parameters_from_dock
from RingyoZumenMaker.drawing_service import DrawingService, request_from_dock
from RingyoZumenMaker.drawing_inputs import DrawingRequest, DrawingError
from RingyoZumenMaker.unified_project import metadata
import processing

project = QgsProject.instance()
layer = QgsVectorLayer('Point?crs=EPSG:6678', '施行地点', 'memory')
layer.dataProvider().addAttributes([QgsField('number', QVariant.Int), QgsField('site', QVariant.String)])
layer.updateFields()
for site, coords in [('現場A', [(0,0),(100,0),(100,100),(0,100)]),
                     ('現場B', [(200,0),(300,0),(300,100),(200,100)])]:
    for number, (x,y) in enumerate(coords):
        f = QgsFeature(layer.fields())
        f.setAttributes([number, site])
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x,y)))
        layer.dataProvider().addFeature(f)
layer.updateExtents()
project.addMapLayer(layer)
root = Path(tempfile.mkdtemp(prefix='output-folder-', dir=QA_ROOT))
print('OUTPUT', root, flush=True)
dock = Main()
dock.crs.setCrs(QgsCoordinateReferenceSystem('EPSG:6678'))
dock.fileName.setFilePath(str(root))
page = dock.add_role_page('land')
page.point_layer.setLayer(layer)
page.filter_exp.setExpression('"site" = \'現場A\'')
page.sort_exp.setExpression('"number"')
page.sokuten_labels.set_expressions(['"number"'], 0)
dock.output_name._drawing_override.setToProperty(QgsProperty.fromField('site'))
config = json.loads(json.dumps(dock.configuration_data()))
restored = Main()
restored.apply_configuration(config)
assert restored.output_name._drawing_override.toProperty().asExpression() == '"site"'
assert not restored.output_name.isEnabled()
assert restored.output_name._drawing_override.vectorLayer() == layer
copied = parameters_from_dock(restored)
assert copied['OUTPUT_NAME'] == 'expression:"site"'
service = DrawingService(request_from_dock(restored))
result = service.run()
assert Path(result['OUTPUT']) == root / '現場A'
assert Path(result['HTML']) == root / '現場A/index.html'
assert Path(result['HTML']).is_file() and Path(result['PROJECT']).is_file()
saved = json.loads(Path(result['CONFIG']).read_text(encoding='utf-8'))
assert Path(saved['output']['directory']) == root
assert saved['output']['folder_name']['property']
print('GUI FIELD OVERRIDE, CONFIG ROUNDTRIP AND NAMED FOLDER EXPORT OK', flush=True)

# Explicitly loading input.config keeps the parent of the named folder.
packaged = QgsProject()
assert packaged.read(result['PROJECT'])
info = metadata(packaged)
assert 'config' not in info and any(k.startswith('input:') for k in info['datasets'])
assert restored.load_config_file(result['CONFIG'])
reopened = DrawingService(request_from_dock(restored))
reopened_result = reopened.run(test=True)
assert Path(reopened_result['OUTPUT']) == root / '現場A'
assert not (root / '現場A/現場A').exists()
print('EXPLICIT CONFIG LOAD RESOLVES PARENT WITHOUT DOUBLE NESTING OK', flush=True)

registry = QgsApplication.processingRegistry()
provider = DrawingProvider()
registry.addProvider(provider)
algorithm = registry.algorithmById(ALGORITHM_ID)
context = QgsProcessingContext()
context.setProject(project)
from processing.gui.BatchAlgorithmDialog import BatchAlgorithmDialog
dialog = BatchAlgorithmDialog(algorithm.create())
panel = dialog.mainWidget()
panel.addRow()
params = dict(copied, OUTPUT=str(root / 'batch'), CREATE_PROJECT=False)
Path(params['OUTPUT']).mkdir()
for name, value in params.items():
    filler = panel.tblParameters.cellWidget(0, panel.parameter_to_column[name])
    filler.setRowValue(0, value, context)
    filler.fillDown()

batch_module = importlib.import_module('processing.gui.BatchPanel')
original_editor = batch_module.QgsExpressionBuilderDialog
class NameExpression:
    def __init__(self, *args, **kwargs): pass
    def exec(self): return True
    def expressionText(self): return "'現場' || (@row_number + 1)"
batch_module.QgsExpressionBuilderDialog = NameExpression
try:
    panel.tblParameters.cellWidget(0, panel.parameter_to_column['OUTPUT_NAME']).calculateByExpression()
finally:
    batch_module.QgsExpressionBuilderDialog = original_editor
for row in range(2):
    row_params, ok = panel.parametersForRow(row, context, warnOnInvalid=True)
    assert ok
    row_result = processing.run(ALGORITHM_ID, row_params, context=context)
    assert Path(row_result['OUTPUT']) == root / 'batch' / ('現場' + str(row+1))
    assert Path(row_result['HTML']).is_file()
print('NATIVE BATCH AUTOFILL CREATES DIFFERENT FOLDERS PER ROW OK', flush=True)

base_params = dict(copied, OUTPUT=str(root), CREATE_PROJECT=False)
# Folder field evaluation happens after the row's filters, not over the layer.
field_result = processing.run(ALGORITHM_ID, dict(base_params, OUTPUT_NAME='field:site'), context=context)
assert Path(field_result['OUTPUT']) == root / '現場A'

# Work-only jobs use the work layer. Preview must not create a new folder.
work_params = {'WORK': layer, 'WORK_FILTER': '"site" = \'現場B\'', 'WORK_ORDER': '"number"',
               'WORK_LABEL': '"number"', 'CRS': 'EPSG:6678', 'OUTPUT': str(root),
               'OUTPUT_NAME': 'expression:"site" || \'_工作物\'', 'CREATE_PROJECT': False}
work_service = DrawingService(algorithm.request(work_params, context))
work_result = work_service.run(test=True)
assert Path(work_result['OUTPUT']) == root / '現場B_工作物'
assert not (root / '現場B_工作物').exists()
assert not (root / '現場B_工作物/input.config').exists()
print('FILTERED ATTRIBUTE REFERENCE AND WORK-ONLY PREVIEW WITHOUT FILES OK', flush=True)

# A row produces a single output directory; ambiguous names and path traversal
# must fail before anything is created or an existing output is changed.
for bad in ('../outside', '..', 'a/b', 'a\\b', 'CON', 'NUL.txt', 'expression:NULL'):
    try:
        DrawingService(algorithm.request(dict(base_params, OUTPUT_NAME=bad), context)).run(test=True)
        raise AssertionError('Invalid folder name accepted: ' + bad)
    except DrawingError:
        pass
try:
    DrawingService(algorithm.request(dict(base_params, LAND_FILTER='', LAND_GROUP='"site"',
                                         OUTPUT_NAME='field:site'), context)).run(test=True)
    raise AssertionError('Multiple names accepted')
except DrawingError:
    pass
blank = DrawingService(algorithm.request(dict(base_params, OUTPUT_NAME=''), context))
assert Path(blank.run(test=True)['OUTPUT']) == root
print('EMPTY NAME COMPATIBILITY, MULTIPLE NAMES AND INVALID NAMES OK', flush=True)
print('PASS', flush=True)
os._exit(0)
