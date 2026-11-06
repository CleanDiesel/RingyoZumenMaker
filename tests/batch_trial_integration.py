"""Verify mixed native batch rows, output-free trials and readable GUI logs."""
from pathlib import Path
import os, tempfile, hashlib

ROOT = Path(__file__).resolve().parents[1]
exec((ROOT / 'tests/measurement_integration.py').read_text(encoding='utf-8').split('from RingyoZumenMaker.main import Main')[0])
from RingyoZumenMaker.processing_provider import DrawingProvider, ALGORITHM_ID
from processing.gui.BatchAlgorithmDialog import BatchAlgorithmDialog
from processing.core.ProcessingResults import resultsList
import processing

project = QgsProject.instance()
def points(name, rings):
    layer = QgsVectorLayer('Point?crs=EPSG:6678', name, 'memory')
    layer.dataProvider().addAttributes([QgsField('number', QVariant.Int), QgsField('part', QVariant.Int)])
    layer.updateFields()
    for part, coords in enumerate(rings):
        for number, (x, y) in enumerate(coords):
            f = QgsFeature(layer.fields())
            f.setAttributes([number, part])
            f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
            assert layer.dataProvider().addFeature(f)
    layer.updateExtents()
    project.addMapLayer(layer)
    return layer

land = points('施行地', [[(0,0),(100,0),(100,100),(0,100)]])
ex = points('除地', [[(20,20),(30,20),(30,30),(20,30)]])
ex_line = points('除地ライン', [[(40,40),(50,40)]])
work = points('付帯工作物', [[(10,10),(40,10)]])
root = Path(tempfile.mkdtemp(prefix='batch-trial-', dir=QA_ROOT))
print('OUTPUT', root, flush=True)
provider = DrawingProvider()
registry = QgsApplication.processingRegistry()
assert registry.addProvider(provider)
algorithm = registry.algorithmById(ALGORITHM_ID)
mode = algorithm.parameterDefinition('RUN_MODE')
assert mode.options() == ['実行', '試算'] and mode.defaultValue() == 0
context = QgsProcessingContext()
context.setProject(project)

class Feedback(QgsProcessingFeedback):
    def __init__(self):
        super().__init__()
        self.lines = []
        self.details = []
    def pushInfo(self, message): self.lines.append(str(message))
    def pushDebugInfo(self, message): self.details.append(str(message))
    @property
    def text(self): return '\n'.join(self.lines)

params = {'LAND':land, 'EX_POLYGON':ex, 'EX_LINE':ex_line, 'WORK':work,
          'CRS':'EPSG:6678', 'SCALE':1000, 'EX_LINE_WIDTH':2, 'WORK_WIDTH':1,
          'MIN_EXCLUSION_A':0, 'CREATE_PROJECT':False}
for prefix in ('LAND','EX_POLYGON','EX_LINE','WORK'):
    params[prefix + '_ORDER'] = '"number"'
    params[prefix + '_LABEL'] = '"number"'

def assert_summary(text, trial):
    assert ('【試算結果】' if trial else '【計算結果】') in text, text
    for expected in ('10,000 m²（1.0000 ha）', '外周長の合計（穴の内周を除く）：400.0 m',
                     'ポリゴン（1件）：100 m²', '延長 10.0 m × 全幅 2 m',
                     '除地面積の合計：120 m²', '差引後の施行地面積：9,880 m²（0.9880 ha）',
                     '付帯工作物（1件）', '延長の合計：30.0 m'):
        assert expected in text, (expected, text)
    assert '施行地面積:' not in text, 'Legacy calculation details must not duplicate the summary'
    assert 'CHILD_INPUTS' not in text and 'Polygonizing' not in text, 'Model internals belong in debug logs'

# A trial accepts no destination and never evaluates output-folder expressions.
feedback = Feedback()
result = processing.run(ALGORITHM_ID, dict(params, RUN_MODE=1, OUTPUT='', OUTPUT_NAME='field:missing'),
                        context=context, feedback=feedback)
assert result == {}, result
assert_summary(feedback.text, True)
assert 'ファイル出力をスキップ' in feedback.text
print('BLANK-DESTINATION TRIAL AND FORMATTED TOTALS OK', flush=True)

# Native enum autofill, validation, mixed trials/exports and HTML registrations.
dialog = BatchAlgorithmDialog(algorithm.create())
panel = dialog.mainWidget()
while panel.batchRowCount() < 4: panel.addRow()
for name, value in dict(params, RUN_MODE=1, OUTPUT=str(root/'unused')).items():
    filler = panel.tblParameters.cellWidget(0, panel.parameter_to_column[name])
    filler.setRowValue(0, value, context)
    filler.fillDown()
assert all(panel.valueForParameter(row, 'RUN_MODE') == 1 for row in range(4))
resultsList.results.clear()
for row, trial in enumerate((True, False, True, False)):
    destination = root / ('row-' + str(row))
    for name, value in {'RUN_MODE':int(trial), 'OUTPUT':str(destination)}.items():
        panel.tblParameters.cellWidget(0, panel.parameter_to_column[name]).setRowValue(row, value, context)
    row_params, valid = panel.parametersForRow(row, context, warnOnInvalid=True)
    assert valid, row_params
    feedback = Feedback()
    result = processing.run(ALGORITHM_ID, row_params, context=context, feedback=feedback)
    assert_summary(feedback.text, trial)
    dialog.loadHtmlResults(result, row + 1)
    if trial:
        assert result == {} and not destination.exists()
    else:
        assert Path(result['HTML']).is_file() and Path(result['CONFIG']).is_file()
        assert '【試算結果】' not in feedback.text
    (root / ('row-' + str(row) + '-log.txt')).write_text(feedback.text, encoding='utf-8')
assert len(resultsList.getResults()) == 2
print('NATIVE MIXED FOUR-ROW BATCH, AUTOFILL AND HTML RESULTS OK', flush=True)

# An existing drawing must remain byte-for-byte and timestamp unchanged.
destination = root/'row-1'
def fingerprint():
    return {str(p.relative_to(destination)):(p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
            for p in destination.rglob('*') if p.is_file()}
before = fingerprint()
feedback = Feedback()
processing.run(ALGORITHM_ID, dict(params, RUN_MODE=1, OUTPUT=str(destination), MIN_EXCLUSION_A=1),
               context=context, feedback=feedback)
assert fingerprint() == before
assert '最小除地面積未満のため差引対象外' in feedback.text
assert '差引後の施行地面積：9,900 m²（0.9900 ha）' in feedback.text
print('EXISTING OUTPUT UNTOUCHED AND MINIMUM DEDUCTION LOG OK', flush=True)

try:
    processing.run(ALGORITHM_ID, dict(params, OUTPUT=''), context=context)
    raise AssertionError('A normal execution requires an output folder')
except QgsProcessingException as error:
    assert '出力フォルダを指定' in str(error), str(error)

# Hole and island geometry retains exterior-only perimeter in the summary.
multipart = points('ドーナツと島', [[(0,0),(100,0),(100,100),(0,100)],
                  [(20,20),(40,20),(40,40),(20,40)],
                  [(120,0),(130,0),(130,10),(120,10)]])
feedback = Feedback()
processing.run(ALGORITHM_ID, {'RUN_MODE':1, 'LAND':multipart, 'LAND_PART':'"part"',
               'LAND_ORDER':'"number"', 'LAND_LABEL':'"number"', 'CRS':'EPSG:6678', 'SCALE':1500},
               context=context, feedback=feedback)
assert '施行地（1件）' in feedback.text and '9,700 m²（0.9700 ha）' in feedback.text
assert '外周長の合計（穴の内周を除く）：440.0 m' in feedback.text, feedback.text
feedback = Feedback()
processing.run(ALGORITHM_ID, {'RUN_MODE':1, 'WORK':work, 'WORK_ORDER':'"number"',
               'WORK_LABEL':'"number"', 'WORK_HAS_WIDTH':False, 'CRS':'EPSG:6678'},
               context=context, feedback=feedback)
assert '延長の合計：30.0 m' in feedback.text and ' ／ 幅 ' not in feedback.text
print('HOLES, MULTIPART EXTERIOR AND WORK-ONLY SUMMARY OK', flush=True)

# Exercise the actual ordinary dock's feedback, for both buttons.
from RingyoZumenMaker.main import Main
warnings = []
QMessageBox.warning = lambda parent, title, text: warnings.append(str(text))
dock = Main()
dock.crs.setCrs(QgsCoordinateReferenceSystem('EPSG:6678'))
dock.scale.setScale(1000)
dock.outputMode.setCurrentIndex(1)
dock.minEx.setValue(0)
gui_folder = root/'gui'
gui_folder.mkdir()
dock.fileName.setFilePath(str(gui_folder))
for role, layer in [('land',land),('ex_polygon',ex),('ex_line',ex_line),('work',work)]:
    page = dock.add_role_page(role)
    page.point_layer.setLayer(layer)
    page.sort_exp.setExpression('"number"')
    page.sokuten_labels.set_expressions(['"number"'], 0)
    if role == 'ex_line': page.haba_spin.setValue(2)
for trial in (True, False):
    dock.on_submit(test=trial)
    assert not warnings, warnings
    assert_summary(dock.outputlog.toPlainText(), trial)
    if trial: assert not list(gui_folder.iterdir())
    else: assert (gui_folder/'index.html').is_file()
print('ORDINARY GUI TRIAL AND EXECUTION SHARE THE READABLE SUMMARY OK', flush=True)
dock.disconnect_project_signals()
registry.removeProvider(provider)
print('PASS', flush=True)
os._exit(0)
