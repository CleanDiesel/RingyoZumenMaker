"""Exercise QGIS's actual Calculate/Add Values by Expression header actions."""
from pathlib import Path
import os, tempfile, importlib

ROOT = Path(__file__).resolve().parents[1]
exec((ROOT / 'tests/measurement_integration.py').read_text(encoding='utf-8').split('from RingyoZumenMaker.main import Main')[0])
from RingyoZumenMaker.processing_provider import DrawingProvider, ALGORITHM_ID
from RingyoZumenMaker.drawing_service import DrawingService
from RingyoZumenMaker.drawing_inputs import DrawingError
from processing.gui.BatchAlgorithmDialog import BatchAlgorithmDialog
import processing

project = QgsProject.instance()
layer = QgsVectorLayer('Point?crs=EPSG:6678', '測点', 'memory')
layer.dataProvider().addAttributes([QgsField('number', QVariant.Int), QgsField('section', QVariant.String),
                                    QgsField('owner', QVariant.String)])
layer.updateFields()
for section, coords in [('A', [(0,0),(100,0),(100,100),(0,100)]),
                        ('B', [(200,0),(300,0),(300,100),(200,100)])]:
    for number, (x,y) in enumerate(coords):
        f = QgsFeature(layer.fields())
        f.setAttributes([number, section, '属性所有者' + section])
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x,y)))
        layer.dataProvider().addFeature(f)
layer.updateExtents()
project.addMapLayer(layer)
provider = DrawingProvider()
QgsApplication.processingRegistry().addProvider(provider)
algorithm = QgsApplication.processingRegistry().algorithmById(ALGORITHM_ID)
context = QgsProcessingContext()
context.setProject(project)
root = Path(tempfile.mkdtemp(prefix='native-autofill-', dir=QA_ROOT))
print('OUTPUT', root, flush=True)
dialog = BatchAlgorithmDialog(algorithm.create())
panel = dialog.mainWidget()
panel.addRow()

def header(name, target=panel):
    return target.tblParameters.cellWidget(0, target.parameter_to_column[name])

params = {'LAND': layer, 'LAND_FILTER': '"section" = \'A\'', 'LAND_ORDER': '"number"',
          'LAND_LABEL': '"number"', 'WORK': layer, 'WORK_GROUP': '"section"',
          'WORK_ORDER': '"number"', 'WORK_LABEL': '"number"',
          'CRS': 'EPSG:6678', 'CREATE_PROJECT': False, 'WORK_INHERIT_ALL': True,
          'OUTPUT': str(root / 'initial')}
for name, value in params.items():
    header(name).setRowValue(0, value, context)
    header(name).fillDown()
for advanced in (False, True):
    panel.toggleAdvancedMode(advanced)
    for param in algorithm.parameterDefinitions():
        if param.name().endswith('_EXPRESSION'):
            assert panel.tblParameters.isColumnHidden(panel.parameter_to_column[param.name()])
print('OVERRIDE COLUMNS REMAIN HIDDEN IN STANDARD AND ADVANCED BATCH VIEWS OK', flush=True)

# Substitute only the modal expression editor. All parameter context creation,
# row variables, expression evaluation and writes use QGIS's native code.
batch_module = importlib.import_module('processing.gui.BatchPanel')
original_editor = batch_module.QgsExpressionBuilderDialog

def populate(filler, text, adding=False):
    class AcceptedExpression:
        def __init__(self, *args, **kwargs): pass
        def setExpectedOutputFormat(self, text): pass
        def exec(self): return True
        def expressionText(self): return text
    batch_module.QgsExpressionBuilderDialog = AcceptedExpression
    try:
        if adding:
            filler.addByExpression()
        else:
            filler.calculateByExpression()
    finally:
        batch_module.QgsExpressionBuilderDialog = original_editor

populate(header('LAND_NAME'), "'施行地' || (@row_number + 1)")
populate(header('LAND_FOREST_OWNER'), "'所有者' || (@row_number + 1)")
populate(header('WORK_FOREST_OWNER'), '@LAND_FOREST_OWNER')
assert panel.batchRowCount() == 2
for row in range(2):
    assert panel.valueForParameter(row, 'LAND_NAME') == '施行地' + str(row+1)
    assert panel.valueForParameter(row, 'WORK_FOREST_OWNER') == '所有者' + str(row+1)
print('NATIVE CALCULATE ACTION, ROW NUMBER AND OTHER COLUMN VARIABLES OK', flush=True)

populate(header('WORK_NAME'), QgsExpression.quotedString('expression:"section"'))
populate(header('WORK_SURVEYOR'), QgsExpression.quotedString('field:owner'))
populate(header('WORK_FOREST_OWNER'), QgsExpression.quotedString('expression:@rz_land_forest_owner'))
for row in range(2):
    header('OUTPUT').setRowValue(row, str(root / ('row-' + str(row+1))), context)
    row_params, ok = panel.parametersForRow(row, context, warnOnInvalid=True)
    assert ok
    result = processing.run(ALGORITHM_ID, row_params, context=context)
    html = Path(result['HTML']).read_text(encoding='utf-8')
    assert '所有者' + str(row+1) in html
    assert '属性所有者A' in html and '属性所有者B' in html
    service = DrawingService(algorithm.request(row_params, context))
    service.run(test=True)
    assert [p.name_edit.text() for p in service.role_pages['work']] == ['A', 'B']
    assert service.attribute_values('work', [service.role_pages['work'][1]])['surveyor'] == ['属性所有者B']
print('NATIVE AUTOFILL WITH PER-GROUP FIELDS, INHERITANCE AND TWO HTML EXPORTS OK', flush=True)

extra_dialog = BatchAlgorithmDialog(algorithm.create())
extra_panel = extra_dialog.mainWidget()
filler = header('LAND_FOREST_OWNER', extra_panel)
start = filler.findStartingRow()
populate(filler, "array('追加所有者A', '追加所有者B')", adding=True)
assert extra_panel.valueForParameter(start, 'LAND_FOREST_OWNER') == '追加所有者A'
assert extra_panel.valueForParameter(start+1, 'LAND_FOREST_OWNER') == '追加所有者B'
print('NATIVE ADD VALUES ACTION WITH AN ARRAY OK', flush=True)

# Previous API commands continue to accept their old expression parameters.
legacy = dict(params, LAND_FOREST_OWNER='固定値', LAND_FOREST_OWNER_EXPRESSION='"owner"', OUTPUT=str(root))
legacy_service = DrawingService(algorithm.request(legacy, context))
legacy_service.run(test=True)
assert legacy_service.attribute_values('land', legacy_service.role_pages['land'])['forest_owner'] == ['属性所有者A']
for text in ('expression:', 'field:'):
    try:
        algorithm.request(dict(params, LAND_FOREST_OWNER=text), context)
        raise AssertionError('Empty deferred input must fail')
    except DrawingError:
        pass
print('OLD COMMAND COMPATIBILITY AND EMPTY EXPRESSION VALIDATION OK', flush=True)
dialog.resize(1600, 700)
dialog.show()
app.processEvents()
dialog.grab().save(str(root / 'native-autofill.png'))
dialog.hide()
print('PASS', flush=True)
os._exit(0)
