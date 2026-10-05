"""Run with the OSGeo4W Python environment; tests real geometry and exports."""
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
BASE = Path(r"C:\Users\m48c7\AppData\Local\Programs\OSGeo4W")
dll_handles = [os.add_dll_directory(str(BASE / part))
               for part in ("bin", "apps/Qt6/bin", "apps/qgis/bin")]
sys.path[:0] = [str(BASE / "apps/qgis/python"), str(BASE / "apps/qgis/python/plugins"),
               str(Path(__file__).resolve().parents[2])]

from qgis.core import (
    QgsApplication, QgsCoordinateReferenceSystem, QgsFeature, QgsField,
    QgsGeometry, QgsProject, QgsRasterLayer, QgsVectorLayer, QgsCoordinateTransform, QgsProperty,
)
from qgis.PyQt.QtCore import QVariant
from qgis.PyQt.QtGui import QFontDatabase, QFont
from qgis.PyQt.QtGui import QImage
from qgis.PyQt.QtWidgets import QMessageBox
from osgeo import gdal
import numpy as np

app = QgsApplication([], False)
app.initQgis()
for path in Path(r"C:\Windows\Fonts").glob("YuGoth*.ttc"):
    QFontDatabase.addApplicationFont(str(path))
app.setFont(QFont("Yu Gothic", 9))

from RingyoZumenMaker.main import Main

messages = []
QMessageBox.warning = lambda parent, title, message: messages.append((title, message))
QA_ROOT = Path(r"C:\Users\m48c7\.codex\visualizations\2026\10\02\01a0fbf6-df38-7290-81f4-3c5a903d47a0")
output_root = Path(tempfile.mkdtemp(prefix="uav-qa-", dir=QA_ROOT))


def vector(name, kind, items):
    layer = QgsVectorLayer(f"{kind}?crs=EPSG:6678", name, "memory")
    layer.dataProvider().addAttributes([QgsField("name", QVariant.String)])
    layer.updateFields()
    for label, wkt in items:
        feature = QgsFeature(layer.fields())
        feature.setAttributes([label])
        feature.setGeometry(QgsGeometry.fromWkt(wkt))
        assert layer.dataProvider().addFeature(feature)
    layer.updateExtents()
    QgsProject.instance().addMapLayer(layer)
    return layer


polygons = vector("元区域", "MultiPolygon", [
    ("区域A", "MULTIPOLYGON(((0 0,100 0,100 100,0 100,0 0)))"),
    ("区域B", "MULTIPOLYGON(((150 0,250 0,250 100,150 100,150 0)))"),
])
exclusions = vector("除地入力", "MultiPolygon", [
    ("除地①", "MULTIPOLYGON(((10 10,20 10,20 25,10 25,10 10)))"),
    ("重複除地", "MULTIPOLYGON(((10 10,20 10,20 25,10 25,10 10)))"),
    ("小さい除地", "MULTIPOLYGON(((30 10,35 10,35 15,30 15,30 10)))"),
])
roads = vector("作業道", "LineString", [("作業道A", "LINESTRING(50 -10,50 110)")])
reference_points = vector("基準点入力", "Point", [
    ("K1", "POINT(20 80)"), ("K2", "POINT(180 80)"), ("対象外", "POINT(300 300)"),
])
ortho_path = output_root / "ortho.tif"
dataset = gdal.GetDriverByName("GTiff").Create(str(ortho_path), 300, 200, 3, gdal.GDT_Byte)
dataset.SetGeoTransform((-25, 1, 0, 150, 0, -1))
dataset.SetProjection(QgsCoordinateReferenceSystem("EPSG:6678").toWkt())
for i, color in enumerate((220, 235, 215), 1):
    dataset.GetRasterBand(i).Fill(color)
dataset = None
ortho = QgsRasterLayer(str(ortho_path), "オルソ")
QgsProject.instance().addMapLayer(ortho)

widget = Main()
widget.crs.setCrs(QgsCoordinateReferenceSystem("EPSG:6678"))
widget.polygon.setLayer(polygons)
widget.jochiPolygon.setLayer(exclusions)
widget.sagyodoLine.setLayer(roads)
widget.olso.setLayer(ortho)
widget.kijunten.setLayer(reference_points)
widget.kijuntenExp.setExpression('"name" IN (\'K1\', \'K2\')')
widget.minKijuntenkan.setValue(20)
for name in ("polygonName", "jochiName", "jochiNameSagyodo"):
    getattr(widget, name).setExpression('"name"')
for name, expression in (("shichoson", "'テスト町'"), ("rinpan", "12"), ("shohan", "60")):
    getattr(widget, name + "Override").setToProperty(QgsProperty.fromExpression(expression))
widget.hukuin.setValue(2)
widget.minJochi.setValue(1)
widget.scale.setScale(2000)
widget.locationScale.setScale(10000)
widget.rinshohan.setText("12-60林班")
widget.shinseibango.setText("123-01")
widget.seizusha.setText("製図者")
widget.sanrinshoyusha.setText("所有者")
widget.isSaveConfig.setCurrentIndex(1)
widget.isIchizu.setChecked(True)
widget.isJochikeisan.setChecked(True)
widget.seizubi.setDate(__import__('qgis.PyQt.QtCore', fromlist=['QDate']).QDate(2026, 10, 2))

output = output_root / "A4"
output.mkdir()
widget.fileName.setFilePath(str(output))
widget.validate_inputs()
widget.calculate_uav()
assert widget.work_area == 20000, widget.work_area
assert widget.exclusion_area == 350, widget.exclusion_area
assert widget.application_area == 19650, widget.application_area
assert len(widget.excluded_small_parts) == 1
assert widget.application_layer.featureCount() == 2
assert list(widget.application_layer.getFeatures())[0].geometry().isMultipart()
assert len(list(widget.application_layer.getFeatures())[0].geometry().asMultiPolygon()) == 2
assert widget.reference_layer.featureCount() == 2
assert widget.reference_distance == 160
assert widget.reference_layer.renderer().symbol().size() == 2
print("geometry, threshold, double-count protection, multipart: OK")

widget.on_submit(test=True)
assert widget.progressBar.value() == 100, messages
assert not list(output.iterdir()), list(output.iterdir())
widget.on_submit(test=False)
assert widget.progressBar.value() == 100, messages
assert (output / "index.html").is_file()
assert (output / "asset/ringyo_zumen.gpkg").is_file()
assert (output / widget.drawings[0]["folder"] / "qgz/application.qgz").is_file()
assert (output / "位置図/qgz/location.qgz").is_file()
# 出力PNGに赤い円が2つあり、300dpiで直径2mm（約24px）になっている。
image = QImage(str(output / "asset/drawing_1_map.png")).convertToFormat(QImage.Format.Format_RGBA8888)
pixels = np.frombuffer(image.constBits().asstring(image.sizeInBytes()), dtype=np.uint8).reshape(image.height(), image.bytesPerLine() // 4, 4)
red = (pixels[:, :, 0] > 240) & (pixels[:, :, 1] < 30) & (pixels[:, :, 2] < 30)
remaining = set(zip(*np.nonzero(red)))
clusters = []
while remaining:
    queue = [remaining.pop()]
    cluster = []
    while queue:
        y, x = queue.pop()
        cluster.append((y, x))
        for neighbor in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
            if neighbor in remaining:
                remaining.remove(neighbor)
                queue.append(neighbor)
    clusters.append(cluster)
clusters = [cluster for cluster in clusters if 300 <= len(cluster) <= 550]
assert len(clusters) == 2, [len(cluster) for cluster in clusters]
for cluster in clusters:
    ys, xs = zip(*cluster)
    assert 22 <= max(xs) - min(xs) + 1 <= 25
    assert 22 <= max(ys) - min(ys) + 1 <= 25
print("two visible red reference circles, diameter 2 mm: OK")
shapefile = next((output / widget.drawings[0]["folder"] / "shp").glob("*.shp"))
shape = QgsVectorLayer(str(shapefile), "成果", "ogr")
assert shape.isValid()
assert shape.featureCount() == 2
features = list(shape.getFeatures())
assert [f["面積ha"] for f in features] == [0.965, 1.0]
assert features[0]["更新ha"] == 2.0
assert features[0]["申請ha"] == 1.96
shape = None
for table in ("layer_1", "layer_2", "layer_3", "layer_4"):
    layer = QgsVectorLayer(f"{output / 'asset/ringyo_zumen.gpkg'}|layername={table}", table, "ogr")
    assert layer.isValid(), table
layer = None
saved = QgsProject()
assert saved.read(str(output / widget.drawings[0]["folder"] / "qgz/application.qgz"))
assert any(layer.type() == ortho.type() for layer in saved.mapLayers().values())
saved_reference = next(layer for layer in saved.mapLayers().values() if layer.name() == "基準点")
assert saved_reference.featureCount() == 2
assert saved_reference.renderer().symbol().size() == 2
saved.clear()
print("trial, shapefile hectares, GPKG layers, QGZ ortho: OK")

data = widget.configuration_data()
assert data["map"]["location_scale"] == 10000
widget.locationScale.setScale(5000)
widget.polygon.setLayer(None)
widget.apply_configuration(data)
assert widget.locationScale.scale() == 10000
assert widget.polygon.currentLayer() == polygons
assert widget.polygonName.expression() == '"name"'
assert widget.kijunten.currentLayer() == reference_points
assert widget.kijuntenExp.expression() == '"name" IN (\'K1\', \'K2\')'
assert widget.minKijuntenkan.value() == 20

for direction in (0, 1):
    directory = output_root / f"A3-{direction}"
    directory.mkdir()
    widget.fileName.setFilePath(str(directory))
    widget.paper.setCurrentIndex(1)
    widget.ichizuDirection.setCurrentIndex(direction)
    widget.isJochikeisan.setChecked(direction == 0)
    widget.on_submit(test=False)
    assert widget.progressBar.value() == 100, messages
    project = QgsProject()
    assert project.read(str(directory / "位置図/qgz/location.qgz"))
    layout = project.layoutManager().layouts()[0]
    page = layout.pageCollection().page(0).pageSize()
    assert (page.width(), page.height()) == ((297, 420) if direction == 0 else (420, 297))
    assert abs(layout.itemById("地図 1").scale() - 10000) < 0.01
    print("A3", direction, "location page/scale", page.width(), page.height())
    project.clear()

# 旧名のshp関連ファイルも次回の生成物バックアップへ入る。
directory = output_root / "A3-1"
(directory / "user-note.txt").write_text("手動追加", encoding="utf-8")
widget.rinshohan.setText("新名称")
widget.backupQgz.setChecked(True)
widget.on_submit(test=False)
assert widget.progressBar.value() == 100, messages
assert (directory / "user-note.txt").is_file()
batch = next((directory / "backup").iterdir())
assert list((batch / "申請区域 - 12-60林班" / "shp").glob("12-60林班*.shp"))
assert list(directory.glob("位置図/新名称 - 位置図.pdf"))
print("backup filename change and manual file preservation: OK")

invalid = vector("ねじれ", "MultiPolygon", [("ねじれ", "MULTIPOLYGON(((0 0,100 100,100 0,0 100,0 0)))")])
widget.polygon.setLayer(invalid)
widget.validate_inputs()
try:
    widget.calculate_uav()
except ValueError as error:
    assert "自己交差" in str(error)
else:
    raise AssertionError("self-intersection was not rejected")
print("self-intersection rejection: OK")
widget.polygon.setLayer(polygons)
widget.polygonName.setExpression("")
widget.jochiName.setExpression("")
widget.jochiNameSagyodo.setExpression("")
widget.validate_inputs()
widget.calculate_uav()
assert not widget.application_layer.labelsEnabled()
assert not widget.exclusion_layer.labelsEnabled()
assert all(not label for label, _ in widget.work_terms)
print("optional blank labels: OK")

# 除地なしと全域除地でも、元地物に対応する属性レコードを保持する。
widget.jochiPolygon.setLayer(None)
widget.sagyodoLine.setLayer(None)
widget.validate_inputs()
widget.calculate_uav()
assert widget.exclusion_area == 0
assert widget.application_area == 20000
assert widget.exclusion_layer.featureCount() == 0
widget.jochiPolygon.setLayer(polygons)
widget.calculate_uav()
assert widget.application_area == 0
assert widget.application_layer.featureCount() == 2
assert all(f.geometry().isEmpty() for f in widget.application_layer.getFeatures())
directory = output_root / "fully-excluded"
directory.mkdir()
widget.fileName.setFilePath(str(directory))
widget.isIchizu.setChecked(False)
widget.backupQgz.setChecked(False)
widget.on_submit(test=False)
assert widget.progressBar.value() == 100, messages
print("no exclusions and fully excluded output: OK")

# 折れ曲がる道の角は直角、両端は平端。閾値ちょうどの部分は採用する。
widget.jochiPolygon.setLayer(None)
corner_road = vector("直角道", "LineString", [("道", "LINESTRING(10 10,60 10,60 60)")])
widget.sagyodoLine.setLayer(corner_road)
widget.minJochi.setValue(2)
widget.calculate_uav()
assert widget.exclusion_area == 200, widget.exclusion_area
assert len(list(widget.exclusion_layer.getFeatures())[0].geometry().asMultiPolygon()[0][0]) == 7
print("flat/miter road buffer and exact threshold: OK")

def reference_error(expression, minimum, message):
    widget.kijuntenExp.setExpression(expression)
    widget.minKijuntenkan.setValue(minimum)
    try:
        widget.calculate_reference_points()
    except ValueError as error:
        assert message in str(error), str(error)
    else:
        raise AssertionError("expected reference point validation error")

reference_error("", 20, "3点以上")
reference_error('"name" = \'K1\'', 20, "2点必要")
reference_error("FALSE", 20, "2点必要")
reference_error('"name" IN (\'K1\', \'K2\')', 161, "未満")
reference_error("CASE WHEN", 20, "基準点指定式")
reference_error('"missing_field" = 1', 20, "基準点指定式")
widget.kijuntenExp.setExpression('"name" IN (\'K1\', \'K2\')')
widget.minKijuntenkan.setValue(160)
widget.calculate_reference_points()
transformed_items = []
transform = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:6678"),
                                   QgsCoordinateReferenceSystem("EPSG:4326"), QgsProject.instance())
for label, wkt in (("K1", "POINT(20 80)"), ("K2", "POINT(180 80)")):
    geometry = QgsGeometry.fromWkt(wkt)
    geometry.transform(transform)
    transformed_items.append((label, geometry.asWkt(16)))
different_crs = vector("異なるCRSの基準点", "Point", transformed_items)
different_crs.setCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
widget.kijunten.setLayer(different_crs)
widget.kijuntenExp.setExpression("")
widget.minKijuntenkan.setValue(159)
widget.calculate_reference_points()
assert abs(widget.reference_distance - 160) < 1e-6
only_two = vector("全地物選択", "Point", [("K1", "POINT(20 80)"), ("K2", "POINT(180 80)")])
widget.kijunten.setLayer(only_two)
widget.kijuntenExp.setExpression("")
widget.calculate_reference_points()
assert widget.reference_layer.featureCount() == 2
widget.kijunten.setLayer(None)
widget.olso.setLayer(None)
widget.calculate_reference_points()
assert widget.reference_layer is None
assert widget.reference_distance is None
optional_output = output_root / "without_optional_layers"
optional_output.mkdir()
widget.fileName.setFilePath(str(optional_output))
widget.isIchizu.setChecked(True)
widget.on_submit(test=False)
assert widget.progressBar.value() == 100, messages
assert None not in widget.result_layers
assert all(layer.name() != "基準点" for layer in widget.result_layers)
assert (optional_output / "asset/drawing_1_map.png").is_file()
assert (optional_output / widget.drawings[0]["folder"] / "qgz/application.qgz").is_file()
assert (optional_output / "位置図/qgz/location.qgz").is_file()
assert 'map_legend' not in (optional_output / "index.html").read_text(encoding="utf-8")
print("exports without reference points or orthophoto, reference legend omitted: OK")
print("reference filter, count, distance, empty expression and errors: OK")
widget.close()
print("QA_OUTPUT=" + str(output_root))
