"""Reproduce cleared legacy fields and QGIS 4.2's missing HTML timestamp."""
from pathlib import Path
import os, tempfile, time

ROOT = Path(__file__).resolve().parents[1]
exec((ROOT / 'tests/measurement_integration.py').read_text(encoding='utf-8').split('from RingyoZumenMaker.main import Main')[0])
from processing.gui.BatchAlgorithmDialog import BatchAlgorithmDialog
from processing.core.ProcessingResults import resultsList
from RingyoZumenMaker.processing_provider import DrawingProvider, ALGORITHM_ID, column_input
from RingyoZumenMaker.processing_compat import install_batch_html_compat
from RingyoZumenMaker.drawing_inputs import DrawingError
from RingyoZumenMaker.drawing_overrides import resolve_values, property_value
import processing

print('QGIS', Qgis.QGIS_VERSION, flush=True)
original_html_loader = BatchAlgorithmDialog.loadHtmlResults
provider = DrawingProvider()
registry = QgsApplication.processingRegistry()
registry.addProvider(provider)
algorithm = registry.algorithmById(ALGORITHM_ID)
patched = BatchAlgorithmDialog.loadHtmlResults
assert patched is not original_html_loader
install_batch_html_compat()
assert BatchAlgorithmDialog.loadHtmlResults is patched
assert column_input('', '""') == ''
assert column_input('文字列', '""') == '文字列'
assert resolve_values('', [], 'forest_compartment') == []
try:
    resolve_values(property_value('', '""'), [], 'forest_compartment')
    raise AssertionError('An explicitly invalid expression must fail')
except DrawingError as error:
    assert 'forest_compartment' in str(error) and "Field '' not found" in str(error), str(error)
for definition in algorithm.parameterDefinitions():
    if definition.name().endswith('_EXPRESSION'):
        assert isinstance(definition, QgsProcessingParameterString)
        assert definition.flags() & Qgis.ProcessingParameterFlag.Hidden
print('EMPTY LEGACY OVERRIDES AND EXPLICIT EXPRESSION ERROR DETAILS OK', flush=True)

project = QgsProject.instance()
layer = QgsVectorLayer('Point?crs=EPSG:6680', '施行地頂点', 'memory')
layer.dataProvider().addAttributes([QgsField('shohan', QVariant.Int), QgsField('vertex_no', QVariant.Int),
                                    QgsField('group_key', QVariant.Int)])
layer.updateFields()
sections = [107, 46, 48, 101, 102, 104, 52, 99, 100]
group = 0
for section_index, section in enumerate(sections):
    number = 0
    for part in range(2 if section_index == 0 else 1):
        x, y = section_index * 100, part * 30
        for px, py in [(x,y), (x+10,y), (x+10,y+10), (x,y+10)]:
            feature = QgsFeature(layer.fields())
            feature.setAttributes([section, number, group])
            feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(px,py)))
            layer.dataProvider().addFeature(feature)
            number += 1
        group += 1
layer.updateExtents()
project.addMapLayer(layer)
root = Path(tempfile.mkdtemp(prefix='batch-results-compat-', dir=QA_ROOT))
print('OUTPUT', root, flush=True)
context = QgsProcessingContext()
context.setProject(project)
dialog = BatchAlgorithmDialog(algorithm.create())
panel = dialog.mainWidget()
params = {'LAND': layer, 'LAND_GROUP': '', 'LAND_PART': '"group_key"',
          'LAND_ORDER': '"vertex_no"', 'LAND_LABEL': '"vertex_no"', 'LAND_NAME': '',
          'LAND_FOREST_COMPARTMENT': '', 'LAND_FOREST_COMPARTMENT_EXPRESSION': '""',
          'LAND_SURVEY_DATE': '2026-11-06', 'LAND_DRAWING_DATE': '2026-11-06',
          'CRS': 'EPSG:6680', 'CREATE_PROJECT': True, 'SHOW_CALCULATIONS': True,
          'SCALE': 2500, 'OUTPUT': str(root), 'OUTPUT_NAME': 'field:shohan'}
for name, value in params.items():
    panel.tblParameters.cellWidget(0, panel.parameter_to_column[name]).setRowValue(0, value, context)
while panel.batchRowCount() < len(sections):
    panel.addRow()
for name in params:
    panel.tblParameters.cellWidget(0, panel.parameter_to_column[name]).fillDown()

resultsList.results.clear()
for row, section in enumerate(sections):
    panel.tblParameters.cellWidget(0, panel.parameter_to_column['LAND_FILTER']).setRowValue(
        row, f'"shohan" = {section}', context)
    row_params, ok = panel.parametersForRow(row, context, warnOnInvalid=True)
    assert ok
    # Missing optional metadata must remain literal empties, including values
    # coming from hidden controls in a batch loaded from an older version.
    request = algorithm.request(row_params, context)
    assert request.config['drawing_attributes']['land']['forest_compartment'] == ''
    result = processing.run(ALGORITHM_ID, row_params, context=context)
    assert Path(result['HTML']) == root / str(section) / 'index.html'
    assert Path(result['HTML']).is_file() and Path(result['PROJECT']).is_file()
    before = len(resultsList.getResults())
    dialog.loadHtmlResults(result, row + 1)
    assert len(resultsList.getResults()) == before + 1
    registered = resultsList.getResults()[-1]
    assert registered.filename == result['HTML']
    assert isinstance(registered.timestamp, time.struct_time)
    print(f'ROW {row+1}/9: OUTPUT AND HTML RESULT REGISTERED OK', flush=True)
assert len(resultsList.getResults()) == 9
dialog.loadHtmlResults({}, 10)
assert len(resultsList.getResults()) == 9
dialog.createSummaryTable([], [{'parameters': params, 'errors': ['検証用のエラー概要']}])
assert len(resultsList.getResults()) == 10
assert Path(resultsList.getResults()[-1].filename).is_file()
print('NINE-ROW EXPORT, HTML CALLBACK AND ERROR SUMMARY OK', flush=True)

registry.removeProvider(provider)
assert BatchAlgorithmDialog.loadHtmlResults is original_html_loader
print('PROVIDER UNLOAD RESTORES QGIS METHOD OK', flush=True)
print('PASS', flush=True)
os._exit(0)
