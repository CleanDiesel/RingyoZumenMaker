import os, sys, tempfile, json, traceback
from pathlib import Path
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
BASE = Path(os.environ.get('QGIS_TEST_BASE', str(Path(sys.executable).resolve().parent.parent)))
os.environ['PROJ_DATA'] = str(BASE / 'share/proj')
os.environ['GDAL_DATA'] = str(BASE / 'share/gdal')
os.environ['PYTHONIOENCODING'] = 'utf-8'
QA_ROOT = Path(os.environ.get('QGIS_TEST_OUTPUT_DIR', tempfile.gettempdir()))
temp_root = QA_ROOT / 'tmp' / 'measurement-test'
temp_root.mkdir(parents=True, exist_ok=True)
os.environ['TEMP'] = os.environ['TMP'] = str(temp_root)
tempfile.tempdir = str(temp_root)
os.environ['QGIS_PREFIX_PATH'] = str(BASE / 'apps/qgis')
os.environ['QT_PLUGIN_PATH'] = str(BASE / 'apps/Qt6/plugins')
handles = [os.add_dll_directory(str(BASE / p)) for p in ('bin', 'apps/Qt6/bin', 'apps/qgis/bin')]
sys.path[:0] = [str(BASE / 'apps/qgis/python'), str(BASE / 'apps/qgis/python/plugins'),
    str(Path(__file__).resolve().parents[2])]
print('Importing QGIS', flush=True)
from qgis.core import *
from qgis.PyQt.QtCore import QVariant
from qgis.PyQt.QtWidgets import QMessageBox
print('Starting QGIS', flush=True)
app = QgsApplication([], False)
app.initQgis()
from qgis.PyQt.QtGui import QFontDatabase, QFont
for font in Path(r'C:\Windows\Fonts').glob('YuGoth*.ttc'): QFontDatabase.addApplicationFont(str(font))
app.setFont(QFont('Yu Gothic', 9))
print('QGIS initialized', flush=True)
print('Importing Processing', flush=True)
from processing.core.Processing import Processing
print('Initializing Processing', flush=True)
from qgis.analysis import QgsNativeAlgorithms
QgsApplication.processingRegistry().addProvider(QgsNativeAlgorithms())
print('Processing initialized', flush=True)
from RingyoZumenMaker.main import Main
print('Plugin imported', flush=True)
from RingyoZumenMaker.unified_project import metadata
messages = []
QMessageBox.warning = lambda parent, title, message: (messages.append(str(message)), print('WARNING', message, flush=True))[-1]
QMessageBox.question = lambda *args: QMessageBox.StandardButton.Save
out = Path(tempfile.mkdtemp(prefix='measurement-upgrade-', dir=QA_ROOT))
print('OUTPUT', out, flush=True)
project = QgsProject.instance()
points = QgsVectorLayer('Point?crs=EPSG:6678', '測点', 'memory')
points.dataProvider().addAttributes([QgsField('number', QVariant.Int), QgsField('name', QVariant.String)])
points.updateFields()
for i, (x, y) in enumerate([(0,0),(100,0),(100,100),(0,100)], 1):
    f = QgsFeature(points.fields()); f.setAttributes([i, 'No.' + str(i)]); f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x,y)))
    assert points.dataProvider().addFeature(f)
points.updateExtents(); project.addMapLayer(points)
try:
    main = Main()
except BaseException:
    print(traceback.format_exc(), flush=True)
    raise
main.crs.setCrs(QgsCoordinateReferenceSystem('EPSG:6678'))
main.fileName.setFilePath(str(out))
main.rinshohan.setText('12林班60小班')
main.add_role_page('land')
main.add_role_page('ex_polygon')
main.add_role_page('work')
main.add_role_page('work')
for i, page in enumerate(main.role_pages['work'], 1): page.name_edit.setText('排水' + str(i))
for page in [*main.shuis, *main.haisuis]:
    page.point_layer.setLayer(points)
    page.sort_exp.setExpression('"number"')
    page.sokuten_labels.set_expressions(['"name"', "'補助' || \"number\""], 0)
main.role_pages['ex_polygon'][0].name_edit.setText('除地A')
ex_points = QgsVectorLayer('Point?crs=EPSG:6678', '除地点', 'memory')
ex_points.dataProvider().addAttributes(list(points.fields())); ex_points.updateFields()
for i, (x,y) in enumerate([(20,20),(30,20),(30,30),(20,30)], 1):
    f=QgsFeature(ex_points.fields()); f.setAttributes([i, 'Ex.'+str(i)]); f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x,y))); ex_points.dataProvider().addFeature(f)
ex_points.updateExtents(); project.addMapLayer(ex_points)
main.role_pages['ex_polygon'][0].point_layer.setLayer(ex_points)
main.attribute_inputs['land']['forest_owner'].setText('山林所有者')
main.attribute_inputs['land']['surveyor'].setText('測量者')
main.attribute_inputs['land']['application_number'].setText('000123-AB')
main.role_pages['work'][1].attribute_inputs['application_number'].setText('000456-CD')
for i, page in enumerate(main.role_pages['work'], 1):
    page.attribute_inputs['forest_owner'].setText('工作物所有者' + str(i))
    page.attribute_inputs['surveyor'].setText('工作物測量者' + str(i))
main.role_pages['work'][1].attribute_inputs['forest_compartment'].setText('"name"')
from qgis.gui import QgsPropertyOverrideButton
assert main.findChildren(QgsPropertyOverrideButton)
assert list(main.basic_groups) == ['座標系', '地図', '位置図', '計算・表示']
assert main.scale.parentWidget() is main.basic_groups['地図']
assert main.locationScale.parentWidget() is main.basic_groups['位置図']
assert main.ketasu.parentWidget() is main.basic_groups['計算・表示']
main.isIchizu.setChecked(True)
main.isJochikeisan.setChecked(True)
main.scale.setScale(1000); main.locationScale.setScale(5000)
main.on_submit()
assert not messages, messages
assert (out/'index.html').is_file(), main.progressBar.value()
files = list((out/'qgz').glob('*.qgz'))
assert len(files) == 1, files
rendered = QgsProject()
assert rendered.read(str(files[0]))
info = metadata(rendered)
assert info and len(info['layouts']) == 4, info
assert 'config' not in info
input_layers = [rendered.mapLayer(v) for k, v in info['datasets'].items() if k.startswith('input:')]
assert sorted(l.name() for l in input_layers) == sorted((points.name(), ex_points.name()))
assert all(l.providerType() == 'ogr' and 'ringyo_zumen.gpkg' in l.source() for l in input_layers)
assert all(l.isValid() for l in rendered.mapLayers().values())
assert main.load_config_file(out/'input.config')
assert main.shuis[0].point_layer.currentLayer() is not None
assert main.scale.scale() == 1000 and main.locationScale.scale() == 5000
print('FIRST EXPORT OK', len(info['layouts']), flush=True)
first_areas = [(p.area, p.length) for p in main.shuis]
# Adjust both independent point-label providers and a layout without saving.
descriptor = next(d for d in info['layouts'] if d['drawing']['folder'] == 'land')
layout = rendered.layoutManager().layoutByName(descriptor['layout'])
map_item = next(i for i in layout.items() if isinstance(i, QgsLayoutItemMap))
map_item.setMapRotation(17)
map_item.setScale(1250)
custom = QgsLayoutItemLabel(layout); custom.setText('手動で追加'); custom.setId('user-note'); layout.addLayoutItem(custom)
custom.attemptMove(QgsLayoutPoint(20, 8)); custom.attemptResize(QgsLayoutSize(40, 6))
key = 'output:shui:' + main.role_pages['land'][0].drawing_uid + ':pt_layer'
layer = rendered.mapLayer(info['datasets'][key])
layer.styleManager().setCurrentStyle('図面:' + descriptor['key'])
aux = layer.auxiliaryLayer()
assert aux is not None
assert len(layer.labeling().subProviders()) == 2
fields = aux.fields().names()
print('AUX', fields, flush=True)
assert any('label_2_position_x' in f for f in fields)
assert aux.isEditable() or aux.startEditing()
f = next(layer.getFeatures())
aux.addFeature(QgsFeature(aux.fields())) if False else None
# QgsAuxiliaryLayer.changeAttributeValue joins on source feature ids.
ix = next(i for i, f in enumerate(fields) if 'label_1_position_x' in f)
iy = next(i for i, f in enumerate(fields) if 'label_2_position_x' in f)
assert layer.startEditing()
assert layer.changeAttributeValue(f.id(), layer.fields().indexFromName('auxiliary_storage_' + fields[ix]), 12.5)
assert layer.changeAttributeValue(f.id(), layer.fields().indexFromName('auxiliary_storage_' + fields[iy]), 18.5)
assert layer.commitChanges()
assert aux.commitChanges()
assert rendered.write()
rendered.clear()
input_layer = main.shuis[0].point_layer.currentLayer()
assert input_layer.startEditing()
source_feature = next(input_layer.getFeatures())
assert input_layer.changeGeometry(source_feature.id(), QgsGeometry.fromPointXY(QgsPointXY(-10, 0)))
main.on_submit()
assert not messages, messages
assert main.progressBar.value() == 100
assert rendered.read(str(files[0]))
info2 = metadata(rendered)
assert info2 and len(info2['layouts']) == 4
layout = rendered.layoutManager().layoutByName(descriptor['layout'])
assert layout.itemById('user-note').text() == '手動で追加'
map_item = next(i for i in layout.items() if isinstance(i, QgsLayoutItemMap))
assert abs(map_item.mapRotation() - 17) < 1e-6
assert abs(map_item.scale() - 1250) < 1e-4
layer = rendered.mapLayer(info2['datasets'][key])
layer.styleManager().setCurrentStyle('図面:' + descriptor['key'])
values = [f.attributes() for f in layer.auxiliaryLayer().getFeatures()]
assert any(12.5 in v and 18.5 in v for v in values), values
assert min(f.geometry().asPoint().x() for f in layer.getFeatures()) == -10
assert '10500' in (out/'index.html').read_text(encoding='utf-8')
assert all(l.isValid() for l in rendered.mapLayers().values())
rendered.clear()
print('REGENERATION AND MULTIPLE LABEL POSITIONS OK', flush=True)
# No-project mode with backup must not move or alter any existing project file.
import hashlib
project_files = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (out/'qgz').rglob('*') if p.is_file()}
main.outputMode.setCurrentIndex(1); main.backupQgz.setChecked(True)
main.on_submit()
assert not messages, messages
assert main.progressBar.value() == 100
assert all(p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest() == digest for p,digest in project_files.items())
assert (out/'input.config').is_file() and not (out/'input.tmp.config').exists()
print('NO PROJECT OUTPUT AND BACKUP OK', flush=True)
# New A3 drawing and location map, plus backward-compatible v2 settings.
out_a3 = out / 'A3'; out_a3.mkdir()
main.outputMode.setCurrentIndex(0)
main.fileName.setFilePath(str(out_a3)); main.paper.setCurrentText('A3'); main.locationPaper.setCurrentText('A3')
main.locationScale.setScale(7500)
main.on_submit()
assert not messages, messages
assert main.progressBar.value() == 100
assert main.paper.currentText() == 'A3' and main.locationScale.scale() == 7500
assert rendered.read(main.last_result['PROJECT'])
info_a3 = metadata(rendered)
for descriptor in info_a3['layouts']:
    layout = rendered.layoutManager().layoutByName(descriptor['layout'])
    size = layout.pageCollection().page(0).pageSize()
    if descriptor['key'] == 'location':
        assert min(size.width(), size.height()) > 290, (size.width(), size.height())
        m = next(i for i in layout.items() if isinstance(i, QgsLayoutItemMap))
        assert abs(m.scale() - 7500) < .02, m.scale()
    else:
        assert 211 < size.width() < 213, size.width()
assert 'work' not in main.attribute_inputs
rendered.clear()
assert [p.attribute_inputs['forest_owner'].text() for p in main.role_pages['work']] == ['工作物所有者1', '工作物所有者2']
assert main.role_pages['work'][1].attribute_inputs['forest_compartment'].text() == '"name"'
for page in main.role_pages['work']:
    assert page.attribute_inputs['forest_owner'].isEnabled()
assert main.role_pages['ex_polygon'][0].is_jochi.isChecked()
assert len(main.role_pages['work']) == 2
assert not list(out_a3.rglob('*.shp'))
html = (out_a3/'index.html').read_text(encoding='utf-8')
assert '振興局' not in html and '測量者' in html and '山林所有者' in html
from lxml import etree as ET
root = ET.fromstring(html, ET.HTMLParser())
options = root.xpath('//*[@id="zumen_type"]/option[@value!=""]')
assert len(options) == 3, [o.get('value') for o in options]
assert root.xpath('//h1')[0].text == '実測図'
assert root.xpath('//*[@id="application_number"]/span[@data-option-panel="land"]')[0].text == '000123-AB'
for i, page in enumerate(main.role_pages['work'], 1):
    panel = 'work_' + page.drawing_uid
    assert root.xpath('//*[@id="application_number"]/span[@data-option-panel="' + panel + '"]')[0].text == (
        '000123-AB' if i == 1 else '000456-CD')
    assert root.xpath('//*[@id="sanrinshoyusha"]/span[@data-option-panel="' + panel + '"]')[0].text == '工作物所有者' + str(i)
    assert root.xpath('//*[@id="sokuryosha"]/span[@data-option-panel="' + panel + '"]')[0].text == '工作物測量者' + str(i)
assert root.xpath('//*[@id="rinshohan"]/span[@data-option-panel="work_' + main.role_pages['work'][1].drawing_uid + '"]')[0].text == '"name"' 
print('A3 EXPORT OK', flush=True)
main.resize(720, 1000); main.show(); main.tabWidget.setCurrentWidget(main.land_tab); app.processEvents()
main.grab().save(str(out/'land-input.png'))
main.tabWidget.setCurrentWidget(main.work_tab); app.processEvents()
main.grab().save(str(out/'work-input.png'))
main.tabWidget.setCurrentWidget(main.basic_tab); app.processEvents()
main.grab().save(str(out/'basic-input.png'))
settings = main.configuration_data()
# A land/exclusions-only export includes exclusion lines in the area calculation
# and excludes ancillary-work lengths entirely.
for page in list(main.role_pages['work']): main.remove_role_page(page)
extra = main.add_role_page('ex_line')
assert extra.isHaba is None
assert extra.haba_spin.isEnabled() and extra.haba_spin.minimum() == .01
extra.update_width_state(False)
assert extra.haba_spin.isEnabled() and extra.values()['has_haba']
main.tabWidget.setCurrentWidget(main.ex_tab); app.processEvents()
main.grab().save(str(out/'exclusion-input.png'))
extra.point_layer.setLayer(main.role_pages['land'][0].point_layer.currentLayer())
extra.filter_exp.setExpression('"number" <= 2'); extra.sort_exp.setExpression('"number"')
extra.sokuten_labels.set_expressions(['"name"'], 0); extra.haba_spin.setValue(2)
land_only = out/'land-only'; land_only.mkdir()
main.fileName.setFilePath(str(land_only)); main.outputMode.setCurrentIndex(1); main.isIchizu.setChecked(False)
main.on_submit()
assert not messages, messages
html = (land_only/'index.html').read_text(encoding='utf-8')
root = ET.fromstring(html, ET.HTMLParser())
assert len(root.xpath('//*[@id="zumen_type"]/option[@value!=""]')) == 1
assert '10180' in html, 'gross - excluded polygon - excluded line'
assert not (land_only/'qgz').exists()
print('LAND AND EXCLUSION LINE ONLY OK', flush=True)
# An ancillary-work-only export must support is_selected() and retain selection
# when input.config is explicitly loaded with the source points available.
main.apply_configuration(settings)
assert main.attribute_inputs['land']['application_number'].text() == '000123-AB'
assert main.role_pages['work'][1].attribute_inputs['application_number'].text() == '000456-CD'
for role in ('land','ex_polygon','ex_line'):
    for page in list(main.role_pages[role]): main.remove_role_page(page)
for page in list(main.role_pages['work'][1:]): main.remove_role_page(page)
work_page = main.role_pages['work'][0]
source = work_page.point_layer.currentLayer()
source.selectByIds([f.id() for f in list(source.getFeatures())[:2]])
work_page.filter_exp.setExpression('is_selected()')
work_only = out/'work-only'; work_only.mkdir()
main.fileName.setFilePath(str(work_only)); main.outputMode.setCurrentIndex(0); main.isIchizu.setChecked(False)
main.on_submit()
assert not messages, messages
assert rendered.read(main.last_result['PROJECT'])
info_work = metadata(rendered)
assert len(info_work['layouts']) == 1
rendered.clear()
source.removeSelection()
assert main.load_config_file(work_only/'input.config')
assert main.role_pages['work'][0].point_layer.currentLayer().selectedFeatureCount() == 2
main.on_submit()
assert not messages, messages
assert main.progressBar.value() == 100
assert rendered.read(main.last_result['PROJECT'])
assert len(metadata(rendered)['layouts']) == 1
rendered.clear()
print('WORK ONLY AND SELECTED POINT RESTORATION OK', flush=True)
# Older configuration files retain their measurements and map into the new roles.
legacy = copy_settings = json.loads(json.dumps(settings))
legacy['version'] = 2; legacy.pop('mode', None); legacy.pop('workflow', None); legacy.pop('drawing_attributes', None)
legacy['basic'].pop('application_number', None)
for group in ('polygons','lines'):
    for item in legacy[group]['items']: item.pop('role', None)
main.application_number._drawing_override.setToProperty(QgsProperty.fromExpression("'OLD-OVERRIDE'"))
main.apply_configuration(legacy)
assert not main.attribute_inputs['land']['application_number'].text()
assert not main.application_number._drawing_override.isActive()
assert len(main.role_pages['land']) == 1 and len(main.role_pages['ex_polygon']) == 1 and len(main.role_pages['work']) == 2
print('LEGACY V2 CONFIG MIGRATION OK', flush=True)
# Earlier shared properties are restored into independent per-drawing inputs.
old = json.loads(json.dumps(settings))
old.pop('attribute_input_mode', None)
for item in old['lines']['items']: item.pop('drawing_attributes', None)
old['drawing_attributes']['work'] = {
    'forest_owner': {'value': '旧共通所有者', 'property': QgsProperty.fromExpression('nonexistent_function()').toVariant()},
    'surveyor': {'value': '', 'property': QgsProperty.fromExpression('@rz_land_surveyor').toVariant()},
}
main.apply_configuration(old)
assert all(p.attribute_inputs['forest_owner'].text() == '旧共通所有者' for p in main.role_pages['work'])
assert all(p.attribute_inputs['surveyor']._drawing_override.toProperty().asExpression() == '@rz_land_surveyor'
           for p in main.role_pages['work'])
assert all(p.attribute_inputs['forest_owner']._drawing_override.isActive() for p in main.role_pages['work'])
main.role_pages['work'][0].attribute_inputs['forest_owner'].setText('個別変更')
assert main.role_pages['work'][1].attribute_inputs['forest_owner'].text() == '旧共通所有者'
round_trip = main.configuration_data()
main.apply_configuration(round_trip)
assert [p.attribute_inputs['forest_owner'].text() for p in main.role_pages['work']] == ['個別変更', '旧共通所有者']
print('INDEPENDENT ATTRIBUTES AND OLD PROPERTY CONFIG MIGRATION OK', flush=True)
# A failure while installing a new drawing restores both replaced and retired
# generated files, while preserving a user-added file.
trial = out/'transaction'; trial.mkdir()
(trial/'index.html').write_text('old', encoding='utf-8')
(trial/'obsolete.png').write_bytes(b'previous')
(trial/'manual.txt').write_text('keep', encoding='utf-8')
main.write_output_manifest(trial/'.ringyo_zumen_outputs.json', {Path('index.html'), Path('obsolete.png')})
stage=out/'transaction-stage'; stage.mkdir(); (stage/'index.html').write_text('new', encoding='utf-8')
main.outputMode.setCurrentIndex(0)
real_replace = os.replace
def fail_install(source, destination):
    if Path(source) == stage/'index.html': raise OSError('injected installation failure')
    return real_replace(source, destination)
os.replace = fail_install
try:
    from RingyoZumenMaker.drawing_inputs import DrawingError
    try:
        main.commit_staged_output(stage, trial, False)
        raise AssertionError('Failed installation must raise a drawing error')
    except DrawingError as error:
        assert str(error).endswith('injected installation failure')
finally:
    os.replace = real_replace
assert (trial/'index.html').read_text(encoding='utf-8') == 'old'
assert (trial/'obsolete.png').read_bytes() == b'previous'
assert (trial/'manual.txt').read_text(encoding='utf-8') == 'keep'
assert not messages, messages
stage2=out/'transaction-stage-2'; stage2.mkdir(); (stage2/'index.html').write_text('new', encoding='utf-8')
assert main.commit_staged_output(stage2, trial, False)
assert not (trial/'obsolete.png').exists()
assert (trial/'index.html').read_text(encoding='utf-8') == 'new'
assert (trial/'manual.txt').read_text(encoding='utf-8') == 'keep'
print('TRANSACTION ROLLBACK AND OBSOLETE OUTPUT CLEANUP OK', flush=True)
(out/'test-output.json').write_text(json.dumps({'output': str(out)}, ensure_ascii=False), encoding='utf-8')
main.rinshohan.setText('読み込み前の値')
assert project.read(str(files[0]))
assert main.rinshohan.text() == '読み込み前の値'
reopened = Main()
assert not reopened.role_pages['land']
assert reopened.load_config_file(out/'input.config')
assert not messages, messages
assert reopened.role_pages['land'][0].point_layer.currentLayer().name() == '測点'
assert reopened.role_pages['ex_polygon'][0].point_layer.currentLayer().name() == '除地点'
assert reopened.attribute_inputs['land']['application_number'].text() == '000123-AB'

# A background added to a saved location theme must keep its theme style.
# The current canvas can be displaying a completely different theme.
from qgis.PyQt.QtGui import QColor
from RingyoZumenMaker.drawing_service import DrawingService, request_from_dock
background_path = out / 'theme-background.geojson'
background_path.write_text(json.dumps({'type': 'FeatureCollection', 'features': [{
    'type': 'Feature', 'properties': {}, 'geometry': {'type': 'Polygon',
        'coordinates': [[[141, 44], [143, 44], [143, 46], [141, 46], [141, 44]]]}}]}), encoding='utf-8')
background = QgsVectorLayer(str(background_path), '位置図テーマの背景', 'ogr')
decoy = QgsVectorLayer(str(background_path), '現在のキャンバスだけの背景', 'ogr')
assert background.isValid() and decoy.isValid()
project.addMapLayers([background, decoy])
manager = background.styleManager()
original_style = manager.currentStyle()
location_style = QgsMapLayerStyle()
location_style.readFromLayer(background)
manager.addStyle('位置図の色', location_style)
manager.setCurrentStyle('位置図の色')
background.setRenderer(QgsSingleSymbolRenderer(QgsFillSymbol.createSimple({'color': '#157b35'})))
manager.setCurrentStyle(original_style)
theme_name = next(d['theme'] for d in metadata(project)['layouts'] if d['key'] == 'location')
themes = project.mapThemeCollection()
record = themes.mapThemeState(theme_name)
entry = QgsMapThemeCollection.MapThemeLayerRecord(background)
entry.isVisible = True
entry.usingCurrentStyle = True
entry.currentStyle = '位置図の色'
record.addLayerRecord(entry)
themes.update(theme_name, record)

class Canvas:
    def layers(self): return [decoy]

class Interface:
    def mapCanvas(self): return Canvas()

reopened.iface = Interface()
service = DrawingService(request_from_dock(reopened), iface=reopened.iface)
backgrounds, overrides = service.location_backgrounds([])
assert backgrounds == [background]
cloned_background = background.clone()
QgsMapLayerStyle(overrides[background.id()]).writeToLayer(cloned_background)
assert cloned_background.renderer().symbol().color() == QColor('#157b35')
reopened.outputMode.setCurrentIndex(0)
reopened.on_submit()
assert reopened.progressBar.value() == 100 and not messages, messages
assert 'config' not in metadata(project)
theme_layers = project.mapThemeCollection().mapThemeVisibleLayers(theme_name)
assert [layer.name() for layer in theme_layers if layer.providerType() == 'ogr' and layer.source().split('|')[0].endswith('theme-background.geojson')] == ['位置図テーマの背景']
assert next(out.glob('* - 位置図.pdf')).stat().st_size > 0
print('LOCATION PDF FOLLOWS SAVED THEME AND BACKGROUND STYLE', flush=True)
project.clear()
print('PASS', flush=True)
os._exit(0)
