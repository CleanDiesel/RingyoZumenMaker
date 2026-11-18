"""Exercise data-defined names, metadata, inheritance and batch exports in QGIS."""
from pathlib import Path
import os, json, tempfile, copy
from lxml import etree as ET

ROOT = Path(__file__).resolve().parents[1]
exec((ROOT / 'tests/measurement_integration.py').read_text(encoding='utf-8').split('from RingyoZumenMaker.main import Main')[0])
from RingyoZumenMaker.main import Main
from RingyoZumenMaker.processing_provider import DrawingProvider, ALGORITHM_ID, parameters_from_dock
from RingyoZumenMaker.drawing_service import DrawingService, request_from_dock
from RingyoZumenMaker.drawing_overrides import property_value, resolve_values
from RingyoZumenMaker.drawing_inputs import DrawingError
from qgis.gui import QgsPropertyOverrideButton

project = QgsProject.instance()
layer = QgsVectorLayer('Point?crs=EPSG:6678', '測点', 'memory')
layer.dataProvider().addAttributes([QgsField('number', QVariant.Int), QgsField('section', QVariant.String),
                                    QgsField('owner', QVariant.String)])
layer.updateFields()
for section, coords in [('A', [(0,0),(100,0),(100,100),(0,100)]),
                        ('B', [(20,20),(30,20),(30,30),(20,30)])]:
    for i, (x, y) in enumerate(coords):
        f = QgsFeature(layer.fields())
        f.setAttributes([i, section, '所有者' + section])
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
        layer.dataProvider().addFeature(f)
layer.updateExtents()
project.addMapLayer(layer)
out = Path(tempfile.mkdtemp(prefix='drawing-overrides-', dir=QA_ROOT))
print('OUTPUT', out, flush=True)
dock = Main()
dock.crs.setCrs(QgsCoordinateReferenceSystem('EPSG:6678'))
dock.fileName.setFilePath(str(out / 'dock'))
dock.outputMode.setCurrentIndex(1)
for role in ('land', 'ex_polygon', 'ex_line', 'work'):
    page = dock.add_role_page(role)
    page.point_layer.setLayer(layer)
    page.filter_exp.setExpression('"section" = ' + QgsExpression.quotedString('A' if role == 'land' else 'B'))
    page.sort_exp.setExpression('"number"')
    page.sokuten_labels.set_expressions(['to_string("number")'], 0)
    page.name_edit._drawing_override.setToProperty(QgsProperty.fromExpression(
        QgsExpression.quotedString(role + '-') + ' || "section"'))

land = dock.role_pages['land'][0]
work = dock.role_pages['work'][0]
owner = dock.attribute_inputs['land']['forest_owner']
owner._drawing_override.setToProperty(QgsProperty.fromField('owner'))
dock.attribute_inputs['land']['surveyor'].setText('測量担当')
dock.attribute_inputs['land']['survey_date']._drawing_override.setToProperty(QgsProperty.fromExpression("to_date('2026-11-05')"))
work.attribute_inputs['forest_owner']._drawing_override.setToProperty(QgsProperty.fromExpression('@rz_land_forest_owner'))
work.attribute_inputs['surveyor']._drawing_override.setToProperty(QgsProperty.fromExpression("'個別測量者'"))

# The actual inheritance menu supports enabling and disabling without changing literals.
button = work.attribute_inputs['survey_company']._drawing_override
button.menu().aboutToShow.emit()
action = next(a for a in button.menu().actions() if a.text() == '施行地の値を継承')
action.trigger()
assert button.isActive() and not work.attribute_inputs['survey_company'].isEnabled()
button.menu().aboutToShow.emit()
assert sum(a.text() == '施行地の値を継承' for a in button.menu().actions()) == 1

config = json.loads(json.dumps(dock.configuration_data(), ensure_ascii=False))
assert config['attribute_input_mode'] == 'data_defined_per_drawing'
assert owner._drawing_override.vectorLayer() == layer
restored = Main()
restored.apply_configuration(config)
assert restored.role_pages['land'][0].name_edit._drawing_override.isActive()
assert restored.attribute_inputs['land']['forest_owner']._drawing_override.toProperty().asExpression() == '"owner"'
assert not restored.attribute_inputs['land']['forest_owner'].isEnabled()
assert restored.role_pages['work'][0].attribute_inputs['forest_owner']._drawing_override.toProperty().asExpression() == '@rz_land_forest_owner'
print('DOCK BUTTONS, INHERITANCE MENU AND CONFIG ROUNDTRIP OK', flush=True)

service = DrawingService(request_from_dock(dock))
result = service.run()
html = Path(result['HTML']).read_text(encoding='utf-8')
for text in ('所有者A', '個別測量者', 'work-B'):
    assert text in html, text
for role in ('land', 'ex_polygon', 'ex_line', 'work'):
    record = service.role_pages[role][0]
    assert record.name_edit.text() == role + ('-A' if role == 'land' else '-B')
    assert record.name_edit.text() in record.line_layer.name()
assert service.attribute_values('land', service.role_pages['land'])['survey_date'] == ['2026年11月05日']
assert service.attribute_values('work', service.role_pages['work'])['forest_owner'] == ['所有者A']
saved = json.loads(Path(result['CONFIG']).read_text(encoding='utf-8'))
assert saved['polygons']['items'][0]['name_property'] == config['polygons']['items'][0]['name_property']
print('REAL EXPORT, FILTERED VALUES, ALL ROLE NAMES AND DATE EXPRESSIONS OK', flush=True)

registry = QgsApplication.processingRegistry()
provider = DrawingProvider()
registry.addProvider(provider)
algorithm = registry.algorithmById(ALGORITHM_ID)
context = QgsProcessingContext()
context.setProject(project)
params = parameters_from_dock(dock)
assert not params.get('DOCK_INPUTS')
assert params['LAND_FOREST_OWNER'] == 'expression:"owner"'
assert params['WORK_FOREST_OWNER'] == 'expression:@rz_land_forest_owner'
assert params['EX_LINE_NAME'].startswith('expression:')
assert not any(key.endswith('_EXPRESSION') for key in params)
params['OUTPUT'] = str(out / 'processing')
import processing
result = processing.run(ALGORITHM_ID, params, context=context)
assert '所有者A' in Path(result['HTML']).read_text(encoding='utf-8')
restored.apply_configuration(json.loads(Path(result['CONFIG']).read_text(encoding='utf-8')))
assert restored.attribute_inputs['land']['survey_date']._drawing_override.isActive()

from processing.gui.BatchAlgorithmDialog import BatchAlgorithmDialog
dialog = BatchAlgorithmDialog(algorithm.create())
panel = dialog.mainWidget()
panel.addRow()
for name in ('LAND_NAME_EXPRESSION', 'LAND_FOREST_OWNER_EXPRESSION', 'WORK_FOREST_OWNER_EXPRESSION'):
    assert panel.tblParameters.isColumnHidden(panel.parameter_to_column[name])
for name, value in params.items():
    filler = panel.tblParameters.cellWidget(0, panel.parameter_to_column[name])
    filler.setRowValue(0, value, context)
    filler.fillDown()
for row in range(2):
    for name, value in {'OUTPUT': str(out / ('batch-' + str(row))),
                        'LAND_FOREST_OWNER': 'バッチ所有者' + str(row),
                        'WORK_NAME': '工作物' + str(row),
                        'WORK_INHERIT_ALL': True}.items():
        panel.tblParameters.cellWidget(0, panel.parameter_to_column[name]).setRowValue(row, value, context)
    row_params, ok = panel.parametersForRow(row, context, warnOnInvalid=True)
    assert ok
    result = processing.run(ALGORITHM_ID, row_params, context=context)
    html = Path(result['HTML']).read_text(encoding='utf-8')
    assert 'バッチ所有者' + str(row) in html
    assert '工作物' + str(row) in html
print('NATIVE PROCESSING, BATCH EXPRESSION AUTOFILL AND TWO EXPORTS OK', flush=True)

# Shared vertices/metadata evaluate only the filtered targets; invalid expressions fail.
page = service.role_pages['land'][0]
assert resolve_values(property_value('', '"owner"'), [page], 'forest_owner') == ['所有者A']
page.item['filter_expression'] = ''
assert resolve_values(property_value('', '"owner"'), [page], 'forest_owner') == ['所有者A', '所有者B']
for expression in ('"missing_field"', 'invalid(', '1 / 0 + invalid_function()'):
    try:
        resolve_values(property_value('fallback', expression), [page], 'forest_owner')
        raise AssertionError('Invalid expression did not fail: ' + expression)
    except DrawingError:
        pass
assert resolve_values(property_value('fallback', 'NULL'), [page], 'forest_owner') == []
print('DEDUPLICATION, NULL AND INVALID EXPRESSION HANDLING OK', flush=True)

# Inheritance follows later edits rather than copying a literal once.
dock.attribute_inputs['land']['forest_owner']._drawing_override.setToProperty(QgsProperty.fromExpression("'更新所有者'"))
for role in ('ex_polygon', 'ex_line', 'work'):
    dock.role_pages[role][0].name_edit._drawing_override.setToProperty(QgsProperty.fromExpression('@rz_land_name'))
updated = DrawingService(request_from_dock(dock))
updated.run(test=True)
assert updated.attribute_values('work', updated.role_pages['work'])['forest_owner'] == ['更新所有者']
assert all(updated.role_pages[role][0].name_edit.text() == 'land-A' for role in ('ex_polygon', 'ex_line', 'work'))
print('LIVE ATTRIBUTE AND ALL ROLE NAME INHERITANCE OK', flush=True)

# The work checkbox applies to figure metadata and reflects individual changes.
dock.attribute_inputs['land']['application_number'].setText('LAND-089')
work.attribute_inputs['application_number'].setText('WORK-001')
other_work = dock.add_role_page('work')
assert not work.inherit_all_attributes.isChecked()
work.inherit_all_attributes.setChecked(True)
assert work.inherit_all_attributes.isChecked()
assert not other_work.inherit_all_attributes.isChecked()
assert all(not widget.isEnabled() for widget in work.attribute_inputs.values())
assert work.name_edit._drawing_override.toProperty().asExpression() == '@rz_land_name'
dock.remove_role_page(other_work)
restored.apply_configuration(json.loads(json.dumps(dock.configuration_data())))
restored_work = restored.role_pages['work'][0]
assert restored_work.inherit_all_attributes.isChecked()
assert restored_work.attribute_inputs['application_number'].text() == 'WORK-001'
dock.fileName.setFilePath(str(out / 'inherit-all'))
all_inherited = DrawingService(request_from_dock(dock))
result = all_inherited.run()
assert all_inherited.attribute_values('work', all_inherited.role_pages['work']) == (
    all_inherited.attribute_values('land', all_inherited.role_pages['land']))
root = ET.HTML(Path(result['HTML']).read_text(encoding='utf-8'))
assert root.xpath('//*[@id="application_number"]/span[starts-with(@data-option-panel, "work_")]/text()') == ['LAND-089']
button = restored_work.attribute_inputs['application_number']._drawing_override
button.menu().aboutToShow.emit()
action = next(a for a in button.menu().actions() if a.text() == '施行地の値を継承')
action.trigger()
assert not restored_work.inherit_all_attributes.isChecked()
assert restored_work.attribute_inputs['application_number'].isEnabled()
assert not restored_work.attribute_inputs['forest_owner'].isEnabled()
action.trigger()
assert restored_work.inherit_all_attributes.isChecked()
restored_work.inherit_all_attributes.setChecked(False)
assert all(widget.isEnabled() for widget in restored_work.attribute_inputs.values())
assert restored_work.attribute_inputs['application_number'].text() == 'WORK-001'
print('WORK CHECKBOX, CONFIG ROUNDTRIP, HTML AND INDIVIDUAL INHERITANCE OK', flush=True)

restored.resize(1000, 850)
restored.tabWidget.setCurrentWidget(restored.work_tab)
restored.show()
app.processEvents()
restored.grab().save(str(out / 'override-controls.png'))
restored.hide()
dock.disconnect_project_signals()
restored.disconnect_project_signals()
print('PASS', flush=True)
os._exit(0)
