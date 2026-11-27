"""Verify multipart vertices, holes, copied attributes and GPKG export."""
from pathlib import Path
import os, runpy, tempfile

ROOT = Path(__file__).resolve().parents[1]
exec((ROOT / 'tests/measurement_integration.py').read_text(encoding='utf-8').split('from RingyoZumenMaker.main import Main')[0])
extract_script = os.environ.get('QGIS_VERTEX_EXTRACT_SCRIPT')
if not extract_script:
    raise RuntimeError('QGIS_VERTEX_EXTRACT_SCRIPT に、プラグイン外の頂点抽出Pythonのパスを指定してください。')
extract = runpy.run_path(extract_script)['extract_multipart_vertices']
layer = QgsVectorLayer('MultiPolygon?crs=EPSG:6678', '施行地', 'memory')
layer.dataProvider().addAttributes([
    QgsField('owner', QVariant.String), QgsField('fid', QVariant.Int),
    QgsField('part_no', QVariant.Int), QgsField('source_part_no', QVariant.String),
])
layer.updateFields()
for wkt, attributes in [
    ('MULTIPOLYGON (((0 0,10 0,10 10,0 10,0 0),(2 2,2 4,4 4,4 2,2 2)),((10 10,20 10,20 20,10 20,10 10)))', ['所有者A', 80, 90, '元値']),
    ('MULTIPOLYGON (((30 0,40 0,40 10,30 10,30 0)))', ['所有者B', 81, 91, '別の元値']),
]:
    f = QgsFeature(layer.fields())
    f.setAttributes(attributes)
    f.setGeometry(QgsGeometry.fromWkt(wkt))
    assert layer.dataProvider().addFeature(f)
layer.updateExtents()
project = QgsProject.instance()
project.addMapLayer(layer)
layer.selectByIds([next(layer.getFeatures()).id()])
out = extract(layer, add_to_project=False)
assert out.crs() == layer.crs()
vertices = list(out.getFeatures())
assert len(vertices) == 16  # All features, including the unselected polygon.
assert out.fields().names()[:4] == ['owner', 'source_fid', 'source_part_no_1', 'source_part_no']
groups = {}
for vertex in vertices:
    groups.setdefault(vertex['group_key'], []).append(vertex)
assert len(groups) == 4
assert sorted(groups) == [0, 1, 2, 3]
for group in groups.values():
    assert [v['ring_vertex_no'] for v in group] == list(range(4))
    assert len({(v.geometry().asPoint().x(), v.geometry().asPoint().y()) for v in group}) == 4
    assert all(v['is_hole'] == (v['ring_no'] > 0) for v in group)
for source in layer.getFeatures():
    points = [v for v in vertices if v['src_fid'] == source.id()]
    assert [v['vertex_no'] for v in points] == list(range(len(points)))
    assert all(v['owner'] == source['owner'] and v['source_fid'] == source['fid'] for v in points)
# A shared coordinate must remain in both parts, or one boundary loses a vertex.
assert sum(v.geometry().asPoint() == QgsPointXY(10, 10) for v in vertices) == 2
print('MULTIPART, HOLES, SHARED VERTICES, ATTRIBUTES AND NUMBERING OK', flush=True)

# Check that grouping/sorting can reconstruct all four boundaries independently.
import processing
context = QgsProcessingContext()
context.setProject(project)
paths = processing.run('native:pointstopath', {'INPUT': out, 'CLOSE_PATH': True,
    'ORDER_EXPRESSION': '"ring_vertex_no"', 'GROUP_EXPRESSION': '"group_key"', 'OUTPUT': 'memory:'}, context=context)['OUTPUT']
assert paths.featureCount() == 4
assert sorted(f.geometry().length() for f in paths.getFeatures()) == [8, 40, 40, 40]
print('PLUGIN GROUPING AND CONNECTION ORDER OK', flush=True)

folder = Path(tempfile.mkdtemp(prefix='multipart-vertices-', dir=QA_ROOT))
options = QgsVectorFileWriter.SaveVectorOptions()
options.driverName = 'GPKG'
result = QgsVectorFileWriter.writeAsVectorFormatV3(out, str(folder / 'vertices.gpkg'), project.transformContext(), options)
assert result[0] == QgsVectorFileWriter.NoError, result
saved = QgsVectorLayer(str(folder / 'vertices.gpkg'), 'saved', 'ogr')
assert saved.isValid() and saved.featureCount() == 16
print('GPKG EXPORT WITH SOURCE ATTRIBUTES OK', flush=True)

# Short display names must not alter separation of outer rings and exclusions.
from RingyoZumenMaker.processing_provider import DrawingProvider, ALGORITHM_ID
from RingyoZumenMaker.drawing_service import DrawingService
provider = DrawingProvider()
QgsApplication.processingRegistry().addProvider(provider)
algorithm = QgsApplication.processingRegistry().algorithmById(ALGORITHM_ID)
params = {'LAND': out, 'LAND_GROUP': '"group_key"', 'LAND_FILTER': '"ring_no" = 0',
          'LAND_ORDER': '"ring_vertex_no"', 'LAND_LABEL': '"vertex_no"', 'LAND_NAME': '施行地',
          'EX_POLYGON': out, 'EX_POLYGON_GROUP': '"group_key"', 'EX_POLYGON_FILTER': '"ring_no" > 0',
          'EX_POLYGON_ORDER': '"ring_vertex_no"', 'EX_POLYGON_LABEL': '"vertex_no"', 'EX_POLYGON_NAME': '除地',
          'CRS': 'EPSG:6678', 'CREATE_PROJECT': False, 'OUTPUT': str(folder)}
request = algorithm.request(params, context)
assert [item['name'] for item in request.config['polygons']['items']] == ['施行地1', '施行地2', '施行地3', '除地']
service = DrawingService(request)
service.run(test=True)
assert sum(page.area for page in service.role_pages['land']) == 300
assert sum(page.area for page in service.role_pages['ex_polygon']) == 4

# Existing point layers with the older composite keys remain supported.
params['LAND_GROUP'] = 'concat("src_fid", \':\', "part_no", \':\', "ring_no")'
old_request = algorithm.request(params, context)
assert [item['name'] for item in old_request.config['polygons']['items']] == ['施行地1', '施行地2', '施行地3', '除地']
assert len({item['drawing_uid'] for item in old_request.config['polygons']['items']}) == 4
print('SHORT NAMES, MULTIPART AREAS, RING EXCLUSIONS AND OLD GROUP KEYS OK', flush=True)

# Feature identity and boundary identity are independent: two outer parts of
# source A form one multipart object, with one label and one calculation term.
params.update(LAND_GROUP='"src_fid"', LAND_PART='"group_key"',
              EX_POLYGON_GROUP='"src_fid"', EX_POLYGON_PART='"group_key"',
              CREATE_PROJECT=True, OUTPUT=str(folder / 'multipart-drawing'))
multipart_request = algorithm.request(params, context)
assert len(multipart_request.config['polygons']['items']) == 3
multipart_service = DrawingService(multipart_request)
multipart_result = multipart_service.run()
lands = multipart_service.role_pages['land']
assert len(lands) == 2
assert [page.area for page in lands] == [200, 100]
assert [page.length for page in lands] == [80, 40]
assert multipart_service.role_pages['ex_polygon'][0].area == 4
for page in lands:
    assert page.line_layer.featureCount() == 1
    assert page.area_layer.featureCount() == 1
    assert not page.line_layer.labeling().settings().labelPerPart
assert len(next(lands[0].line_layer.getFeatures()).geometry().asMultiPolyline()) == 2
assert len(next(lands[0].area_layer.getFeatures()).geometry().asMultiPolygon()) == 2
assert Path(multipart_result['PROJECT']).is_file()
print('SINGLE MULTIPART FEATURE, LABEL AND CALCULATION WITH QGZ EXPORT OK', flush=True)

from RingyoZumenMaker.main import Main
from RingyoZumenMaker.processing_provider import parameters_from_dock
dock = Main()
project.addMapLayer(out)
dock.crs.setCrs(QgsCoordinateReferenceSystem('EPSG:6678'))
dock.fileName.setFilePath(str(folder))
page = dock.add_role_page('land')
page.point_layer.setLayer(out)
first_id = next(layer.getFeatures()).id()
page.filter_exp.setExpression(f'"src_fid" = {first_id} AND "ring_no" = 0')
page.part_exp.setExpression('"group_key"')
page.sort_exp.setExpression('"ring_vertex_no"')
page.sokuten_labels.set_expressions(['"vertex_no"'], 0)
copied = parameters_from_dock(dock)
assert copied['LAND_PART'] == '"group_key"'
restored = Main()
restored.apply_configuration(dock.configuration_data())
assert restored.role_pages['land'][0].part_exp.expression() == '"group_key"'
from RingyoZumenMaker.drawing_service import request_from_dock
gui_service = DrawingService(request_from_dock(restored))
gui_service.run(test=True)
assert len(gui_service.role_pages['land']) == 1
assert gui_service.role_pages['land'][0].area == 200
assert gui_service.role_pages['land'][0].length == 80

# The same three outer rings can be two logical polygons (A is multipart),
# three independent polygons, or a single multipart polygon by changing only
# the object grouping expression. Boundary grouping stays the same.
page.filter_exp.setExpression('"ring_no" = 0')
page.object_exp.setExpression('"src_fid"')
two_objects = DrawingService(request_from_dock(dock))
two_objects.run(test=True)
assert [p.area for p in two_objects.role_pages['land']] == [200, 100]
copied = parameters_from_dock(dock)
assert copied['LAND_GROUP'] == '"src_fid"'
assert copied['LAND_PART'] == '"group_key"'
restored.apply_configuration(dock.configuration_data())
assert restored.role_pages['land'][0].object_exp.expression() == '"src_fid"'
page.object_exp.setExpression('"group_key"')
separate_objects = DrawingService(request_from_dock(dock))
separate_objects.run(test=True)
assert [p.area for p in separate_objects.role_pages['land']] == [100, 100, 100]
page.object_exp.setExpression('')
single_object = DrawingService(request_from_dock(dock))
single_object.run(test=True)
assert len(single_object.role_pages['land']) == 1
assert single_object.role_pages['land'][0].area == 300
assert single_object.role_pages['land'][0].length == 120
page.object_exp.setExpression('"src_fid"')
original_warning = QMessageBox.warning
def fail_warning(parent, title, message):
    raise AssertionError(message)
QMessageBox.warning = fail_warning
try:
    dock.on_submit(test=True)
finally:
    QMessageBox.warning = original_warning
assert page.area == 300 and page.length == 120
print('GUI CHOICE OF ONE MULTIPART, TWO OBJECTS OR THREE SEPARATE POLYGONS OK', flush=True)

# Store the original GUI registration in input.config when it expands into
# several logical output polygons with different internal UIDs.
page.object_exp.setExpression('"src_fid"')
dock.outputMode.setCurrentIndex(0)
dock.fileName.setFilePath(str(folder / 'gui-grouped-project'))
grouped_service = DrawingService(request_from_dock(dock))
grouped_result = grouped_service.run()
packaged = QgsProject()
assert packaged.read(grouped_result['PROJECT'])
from RingyoZumenMaker.unified_project import metadata
info = metadata(packaged)
assert 'config' not in info and any(k.startswith('input:') for k in info['datasets'])
assert restored.load_config_file(grouped_result['CONFIG'])
saved_item = restored.configuration_data()['polygons']['items'][0]
assert saved_item['drawing_uid'] == page.drawing_uid
assert saved_item['object_expression'] == '"src_fid"'
assert saved_item['part_expression'] == '"group_key"'
assert saved_item['layer']['id'] == out.id()
reopened = DrawingService(request_from_dock(restored))
reopened.run(test=True)
assert [p.area for p in reopened.role_pages['land']] == [200, 100]
print('GUI GROUPED QGIS PROJECT REGENERATES FROM EXPLICIT CONFIG OK', flush=True)
print('GUI MULTIPART REGISTRATION, CONFIG ROUNDTRIP AND PROCESSING COPY OK', flush=True)
print('PASS', flush=True)
os._exit(0)
