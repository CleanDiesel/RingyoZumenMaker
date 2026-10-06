"""Integration test with real QGIS widgets, expressions, geometries and saved projects."""
import json
import os
import sys
import tempfile
import xml.etree.ElementTree as XML
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
BASE = Path(r"C:\Users\m48c7\AppData\Local\Programs\OSGeo4W")
dll_handles = [os.add_dll_directory(str(BASE / part)) for part in ("bin", "apps/Qt6/bin", "apps/qgis/bin")]
sys.path[:0] = [str(BASE / "apps/qgis/python"), str(BASE / "apps/qgis/python/plugins"),
               str(Path(__file__).resolve().parents[2])]
from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsFeature, QgsField,
                       QgsGeometry, QgsProject, QgsProperty, QgsVectorLayer, QgsRasterLayer)
from qgis.PyQt.QtCore import QVariant, QDate
from qgis.PyQt.QtGui import QFontDatabase, QFont
from qgis.PyQt.QtWidgets import QMessageBox
from osgeo import gdal
from lxml import etree as ET

app = QgsApplication([], False)
app.initQgis()
for path in Path(r"C:\Windows\Fonts").glob("YuGoth*.ttc"):
    QFontDatabase.addApplicationFont(str(path))
app.setFont(QFont("Yu Gothic", 9))
from RingyoZumenMaker.main import Main
from RingyoZumenMaker.uav_inputs import LAYER_INPUTS, ATTRIBUTE_INPUTS, prefixed
sys.excepthook = sys.__excepthook__

messages = []
QMessageBox.warning = lambda parent, title, message: messages.append((title, message))
QA_ROOT = Path(r"C:\Users\m48c7\.codex\visualizations\2026\10\02\01a0fbf6-df38-7290-81f4-3c5a903d47a0")
output_root = Path(tempfile.mkdtemp(prefix="multi-uav-qa-", dir=QA_ROOT))


def vector(name, kind, items):
    layer = QgsVectorLayer(f"{kind}?crs=EPSG:6678", name, "memory")
    layer.dataProvider().addAttributes([QgsField("name", QVariant.String), QgsField("width", QVariant.Double)])
    layer.updateFields()
    for label, wkt, width in items:
        feature = QgsFeature(layer.fields())
        feature.setAttributes([label, width])
        feature.setGeometry(QgsGeometry.fromWkt(wkt))
        assert layer.dataProvider().addFeature(feature)
    layer.updateExtents()
    QgsProject.instance().addMapLayer(layer)
    return layer


polygons = vector("元区域", "MultiPolygon", [
    ("区域A", "MULTIPOLYGON(((0 0,100 0,100 100,0 100,0 0)))", 1),
    ("区域B", "MULTIPOLYGON(((150 0,250 0,250 100,150 100,150 0)))", 2)])
singles = vector("単一区域", "MultiPolygon", [
    ("同名", "MULTIPOLYGON(((0 0,20 0,20 20,0 20,0 0)),((25 0,30 0,30 20,25 20,25 0)))", 2),
    ("同名", "MULTIPOLYGON(((40 0,50 0,50 20,40 20,40 0)))", 3)])
exclusions = vector("除地", "MultiPolygon", [
    ("除地①", "MULTIPOLYGON(((10 10,20 10,20 25,10 25,10 10)))", 1),
    ("重複除地", "MULTIPOLYGON(((10 10,20 10,20 25,10 25,10 10)))", 1),
    ("小さい除地", "MULTIPOLYGON(((30 10,35 10,35 15,30 15,30 10)))", 1)])
roads = vector("除地道", "MultiLineString", [("排水1", "MULTILINESTRING((50 -10,50 110))", 2)])
single_lines = vector("単一道", "MultiLineString", [
    ("道A", "MULTILINESTRING((10 10,60 10,60 60),(70 10,90 10))", 2),
    ("道B", "MULTILINESTRING((110 0,110 40))", 3)])
points = vector("基準点", "Point", [("K1", "POINT(20 80)", 20), ("K2", "POINT(180 80)", 20)])
ortho_path = output_root / "original.tif"
ds = gdal.GetDriverByName("GTiff").Create(str(ortho_path), 300, 200, 3, gdal.GDT_Byte)
ds.SetGeoTransform((-25, 1, 0, 150, 0, -1))
ds.SetProjection(QgsCoordinateReferenceSystem("EPSG:6678").toWkt())
for index, color in enumerate((220, 235, 215), 1):
    ds.GetRasterBand(index).Fill(color)
ds = None
ortho = QgsRasterLayer(str(ortho_path), "オルソ")
QgsProject.instance().addMapLayer(ortho)

widget = Main()
assert widget.polygon.currentLayer() is None
assert not hasattr(widget, "singlePolygon")
assert not hasattr(widget, "toolBox")
assert widget.singleLineWidth.decimals() == 2
assert not widget.hukuin.isEnabled()
widget.crs.setCrs(QgsCoordinateReferenceSystem("EPSG:6678"))
widget.scale.setScale(2000)
widget.locationScale.setScale(10000)
widget.rinshohan.setText("12-60林班")
widget.shinseibango.setText("123-01")
widget.seizusha.setText("製図者")
widget.sanrinshoyusha.setText("所有者")
widget.seizubi.setDate(QDate(2026, 10, 5))
for prefix in ("", "singleLine"):
    getattr(widget, prefixed(prefix, "shichoson")).setText("テスト町")
    getattr(widget, prefixed(prefix, "rinpan")).setValue(12)
    getattr(widget, prefixed(prefix, "shohan")).setValue(60)
    getattr(widget, prefixed(prefix, "jigyoCode")).setText("123")
for name, layer in (("polygon", polygons), ("jochiPolygon", exclusions), ("sagyodoLine", roads),
                    ("singleLine", single_lines), ("kijunten", points), ("olso", ortho)):
    getattr(widget, name).setLayer(layer)
for field, combo_name in widget._override_bindings.items():
    button = getattr(widget, field + "Override")
    combo = getattr(widget, combo_name)
    selected_layer = combo.currentLayer()
    assert button.isEnabled(), field
    assert button.vectorLayer() is selected_layer, field
    combo.setLayer(None)
    assert not button.isEnabled(), field
    combo.setLayer(selected_layer)
    assert button.isEnabled(), field
for name in ("polygonName", "jochiName", "jochiNameSagyodo", "singleLineName"):
    getattr(widget, name).setExpression('"name"')
for name in ("hukuin", "singleLineWidth"):
    button = getattr(widget, name + "Override")
    button.setToProperty(QgsProperty.fromField("width"))
    widget.update_override_enabled(name)
    assert button.vectorLayer() is (roads if name == "hukuin" else single_lines)
    assert not getattr(widget, name).isEnabled()
widget.singleLineSeizubi2Override.setToProperty(QgsProperty.fromExpression("to_date('2026-10-01')"))
widget.singleLineSeizusha2Override.setToProperty(QgsProperty.fromExpression("'地物ごとの製図者'"))
widget.singleLineSanrinshoyusha2Override.setToProperty(QgsProperty.fromExpression("'所有者' || \"name\""))
widget.minKijuntenkanOverride.setToProperty(QgsProperty.fromField("width"))
widget.minJochi.setValue(1)
widget.isJochikeisan.setChecked(True)
widget.isIchizu.setChecked(True)
widget.assignmentOlso.setFilePath(str(ortho_path))
widget.isSaveConfig.setCurrentIndex(1)
output = output_root / "mixed"
output.mkdir()
widget.fileName.setFilePath(str(output))
widget.validate_inputs()
widget.calculate_uav()
assert len(widget.drawings) == 3
assert widget.work_area == 20000
assert widget.exclusion_area == 350
assert widget.application_area == 19650
assert len(widget.excluded_small_parts) == 1
assert widget.drawings[1]["application_layer"].featureCount() == 1
assert next(widget.drawings[1]["application_layer"].getFeatures())["延長m"] == 120
assert widget.drawings[1]["application_layer"].geometryType() == 1
assert widget.drawings[2]["application_layer"].geometryType() == 1
assert next(widget.drawings[1]["application_layer"].getFeatures()).geometry().length() == 120
assert len(next(widget.drawings[1]["application_layer"].getFeatures()).geometry().asMultiPolyline()) == 2
assert widget.line_calculation_label("排水1", 89, 1) == "排水1・延長89m × 幅1m"
assert len(set(d["folder"].casefold() for d in widget.drawings)) == 3
print("UI setup, property overrides, filter bindings and 3 drawings: OK", flush=True)

data = widget.configuration_data()
json.dumps(data)
restored = Main()
assert not restored.apply_configuration(data)
assert restored.singleLineWidthOverride.toProperty().field() == "width"
assert restored.singleLine.currentLayer() is single_lines
assert restored.minKijuntenkan.value() == 20
restored.close()
widget.on_submit(test=True)
assert widget.progressBar.value() == 100, messages
assert not list(output.iterdir())
widget.on_submit(test=False)
assert widget.progressBar.value() == 100, messages
assert (output / "index.html").is_file()
assert (output / "backup").is_dir()
assert not (output / "共通").exists()
assert (output / "asset/QGISで図面を編集する方法.md").is_file()
assert not list(output.rglob("*.zip"))
assert len(list(output.glob("*/shp/*.shp"))) == 3
assert len(list(output.glob("qgz/*.qgz"))) == 1
assert len(list(output.glob("位置図/*位置図.pdf"))) == 1
assert len(list(output.rglob("ringyo_zumen.gpkg"))) == 1
assert (output / "qgz/ringyo_zumen.gpkg").is_file()
assert len(list(output.glob("オルソ/*オルソ.tif"))) == 1
html = ET.parse(str(output / "index.html"), ET.HTMLParser())
assert len(html.xpath("//div[@class='drawing-sheet main_container']")) == 3
ids = html.xpath("//@id")
assert len(ids) == len(set(ids))
assert "延長100m" in "".join(html.getroot().itertext())
for sheet in html.xpath("//div[@data-drawing]"):
    labels = sheet.xpath(".//div[@class='calc-label']/text()")
    assert labels == (["更新面積", "除地", "申請面積"] if sheet.get("data-drawing") == "1" else ["地物名", "幅", "延長"])
    assert all((output / source).is_file() for source in sheet.xpath(".//img/@src"))
assert any("120 m" == text for text in html.xpath("//span[@data-latex]/text()"))
(output_root / "html-qa.json").write_text(json.dumps({
    "script": html.xpath("//script[not(@src)]/text()")[0],
    "formulas": html.xpath("//@data-latex"),
    "titles": html.xpath("//div[@data-drawing]/@data-title"),
}, ensure_ascii=False), encoding="utf-8")
for drawing in widget.drawings:
    folder = output / drawing["folder"]
    shape = QgsVectorLayer(str(next((folder / "shp").glob("*.shp"))), "成果", "ogr")
    assert shape.isValid()
    assert shape.featureCount() == (2 if drawing["kind"] == "申請区域" else 1)
    fields = shape.fields().names()
    if drawing["kind"] == "申請区域":
        assert "更新ha" in fields and "申請ha" in fields
        assert next(shape.getFeatures())["申請ha"] == 1.96
    else:
        assert not any(field in fields for field in ("更新ha", "申請ha", "面積ha"))
        iterator = shape.getFeatures()
        single_feature = next(iterator)
        iterator.close()
        assert isinstance(single_feature["延長m"], int)
        assert shape.fields().field("幅m").precision() == 2
    if drawing["kind"] == "付帯作工物":
        assert shape.geometryType() == 1
        iterator = shape.getFeatures()
        f = next(iterator)
        iterator.close()
        assert f["幅m"] == (2 if drawing["title"] == "道A" else 3)
        assert f["延長m"] == (120 if drawing["title"] == "道A" else 40)
        assert f["製図日"] == QDate(2026, 10, 1)
        assert f["製図者"] == "地物ごとの製図者"
    project = QgsProject()
    assert project.read(str(output / "qgz/図面編集・再作成.qgz"))
    assert any(layer.name() == "基準点" for layer in project.mapLayers().values())
    reference = next(layer for layer in project.mapLayers().values() if layer.name() == "基準点")
    assert reference.geometryType() == 0 and reference.featureCount() == 2
    assert reference.providerType() == "ogr"
    assert len(project.layoutManager().layouts()) == 4
    assert len(project.mapThemeCollection().mapThemes()) == 4
    maps = [item for item in project.layoutManager().layoutByName(drawing['folder']).items()
            if isinstance(item, __import__('qgis.core', fromlist=['QgsLayoutItemMap']).QgsLayoutItemMap)]
    center = maps[0].extent().center()
    assert abs(center.x() - ortho.extent().center().x()) < 1e-6
    assert abs(center.y() - ortho.extent().center().y()) < 1e-6
    for layer in project.mapLayers().values():
        assert layer.isValid(), layer.name()
        if layer.providerType() == "ogr":
            assert Path(layer.source().split('|')[0]).resolve() == (output / "qgz/ringyo_zumen.gpkg").resolve()
        if drawing["kind"] == "付帯作工物" and layer.name() == "付帯作工物":
            assert layer.geometryType() == 1 and layer.featureCount() == 1
        if layer.providerType() == "gdal":
            assert Path(layer.source()).resolve() == next(output.glob('オルソ/*オルソ.tif')).resolve()
    project.clear()
    assert project.read(str(output / "qgz/図面編集・再作成.qgz"))
    layout = project.layoutManager().layoutByName("位置図")
    assert abs(layout.itemById("地図 1").scale() - 10000) < 0.01
    location_map = layout.itemById("地図 1")
    arrow_path = Path(layout.itemById("方位記号").picturePath())
    assert arrow_path.is_file()
    assert arrow_path.resolve() == (output / "qgz/houi2.svg").resolve()
    for layer in location_map.layers():
        assert layer.isValid()
        assert Path(layer.source().split('|')[0]).resolve() == (output / "qgz/ringyo_zumen.gpkg").resolve()
    location_styles = project.mapThemeCollection().mapThemeStyleOverrides("位置図")
    assert len(location_styles) == 3
    assert all("図面:location" in layer.styleManager().styles() for layer in location_map.layers())
    from qgis.core import QgsLayoutExporter
    assert QgsLayoutExporter(layout).renderPageToImage(0, dpi=100).save(str(output_root / "location-preview.png"))
    project.clear()
    shape = None
print("real exports: 3 SHP, unified QGZ/4 themes, shared GPKG/PDF/ortho, no ZIP: OK", flush=True)

# Rename drawings, remove features and retain manually added files during full backup.
previous = json.loads((output / ".ringyo_zumen_outputs.json").read_text(encoding="utf-8"))["paths"]
manual = output / widget.drawings[1]["folder"] / "user-note.txt"
manual.write_text("手動追加", encoding="utf-8")
widget.rinshohan.setText("新しい名称")
widget.singleLineFilter.setExpression('"width" = 2')
widget.backupQgz.setChecked(True)
widget.on_submit(test=False)
assert widget.progressBar.value() == 100, messages
batch = next((output / "backup").iterdir())
assert all((batch / relative).is_file() for relative in previous)
assert manual.read_text(encoding="utf-8") == "手動追加"
assert not (batch / manual.relative_to(output)).exists()
assert len(list(output.glob("*/shp/*.shp"))) == 2
assert (batch / ".ringyo_zumen_outputs.json").is_file()
print("backup: every old generated file, removed features, renamed folders, manual file untouched: OK", flush=True)

# Either mode alone is allowed; an empty filter in both modes is rejected.
widget.polygon.setLayer(None)
widget.isIchizu.setChecked(False)
widget.kijunten.setLayer(None)
widget.validate_inputs()
widget.calculate_uav()
assert len(widget.drawings) == 1
assert all(d["exclusion_area"] == 0 for d in widget.drawings)
widget.singleLineFilter.setExpression("FALSE")
try:
    widget.validate_inputs()
except ValueError as error:
    assert "少なくとも1" in str(error)
else:
    raise AssertionError("Both modes omitted was accepted")
widget.singleLine.setLayer(None)
widget.polygon.setLayer(polygons)
widget.validate_inputs()
widget.calculate_uav()
assert len(widget.drawings) == 1
print("aggregate-only, singles-only, no deduction on singles, both omitted rejected: OK", flush=True)

# Invalid overrides must fail rather than silently use the input to their left.
widget.hukuinOverride.setToProperty(QgsProperty.fromExpression("'bad-width'"))
try:
    widget.calculate_uav()
except ValueError as error:
    assert "hukuin" in str(error)
else:
    raise AssertionError("Invalid width was silently accepted")
widget.hukuinOverride.setToProperty(QgsProperty.fromField("width"))
widget.polygonFilter.setExpression('"name" = \'区域A\'')
widget.sagyodoFilter.setExpression("FALSE")
widget.jochiFilter.setExpression("FALSE")
widget.calculate_uav()
assert widget.application_area == 10000
assert widget.application_layer.featureCount() == 1
widget.singleLine.setLayer(single_lines)
widget.singleLineFilter.setExpression('"width" = 2')
widget.calculate_uav()
widget.activate_drawing(widget.drawings[-1])
widget.isJochikeisan.setChecked(False)
sheet = widget.build_uav_html_sheet(1, "test.png")
assert sheet.xpath(".//div[@class='calc-label']/text()") == ["地物名", "幅", "延長"]
assert "120 m" in "".join(sheet.itertext())
assert "ha" not in "".join(sheet.xpath(".//div[contains(@class, 'calc_area')]")[0].itertext())
print("invalid override rejected, aggregate filters, single direct area display: OK", flush=True)

fractional = vector("端数ライン", "LineString", [("端数", "LINESTRING(0 0,10.999 0)", 1.237)])
widget.polygon.setLayer(None)
widget.singleLine.setLayer(fractional)
widget.singleLineFilter.setExpression("")
widget.validate_inputs()
widget.calculate_uav()
fractional_feature = next(widget.application_layer.getFeatures())
assert fractional_feature["延長m"] == 10
assert fractional_feature["幅m"] == 1.24
assert abs(fractional_feature.geometry().length() - 10.999) < 1e-8
assert not any(name in widget.application_layer.fields().names() for name in ("面積ha", "申請ha", "更新ha"))
print("fractional length floors to integer metres, width rounds to two decimals: OK", flush=True)
line_only_output = output_root / "line-only"
line_only_output.mkdir()
widget.fileName.setFilePath(str(line_only_output))
widget.olso.setLayer(None)
widget.assignmentOlso.setFilePath("")
widget.isIchizu.setChecked(True)
widget.on_submit(test=False)
assert widget.progressBar.value() == 100, messages
assert (line_only_output / "qgz/図面編集・再作成.qgz").is_file()
line_project = QgsProject()
assert line_project.read(str(line_only_output / "qgz/図面編集・再作成.qgz"))
assert all(layer.isValid() for layer in line_project.mapLayers().values())
line_project.clear()
print("horizontal line only, no orthophoto or reference points: exports OK", flush=True)

# Old UAV v3 expressions migrate into the new property buttons.
legacy = {"format": "RingyoZumenMaker.config", "version": 3,
          "basic": {"seizusha": "旧製図者", "seizubi": "2026-10-01"},
          "expressions": {"shichoson": "'旧町'", "rinpan": "123", "polygonName": '"name"'},
          "region": "上川", "output": {}}
legacy_widget = Main()
legacy_widget.apply_configuration(legacy)
assert legacy_widget.shichosonOverride.isActive()
assert legacy_widget.rinpanOverride.toProperty().asExpression() == "123"
assert legacy_widget.shinkokyoku.currentText() == "上川"
legacy_widget.close()
print("v3 configuration expression migration: OK", flush=True)

# A failed commit restores old generated files and the original manifest.
from RingyoZumenMaker import main as main_module
transaction = output_root / "transaction"
transaction.mkdir()
(transaction / "asset").mkdir()
(transaction / "index.html").write_text("old html", encoding="utf-8")
(transaction / "asset/map.png").write_bytes(b"old map")
widget.write_output_manifest(transaction / ".ringyo_zumen_outputs.json", [Path("index.html"), Path("asset/map.png")])
original_manifest = (transaction / ".ringyo_zumen_outputs.json").read_bytes()
stage = output_root / "stage"
stage.mkdir()
(stage / "index.html").write_text("new html", encoding="utf-8")
(stage / "asset").mkdir()
(stage / "asset/map.png").write_bytes(b"new map")
replace = main_module.os.replace
def failing_replace(source, target):
    if Path(source) == stage / "index.html":
        raise OSError("simulated output lock")
    return replace(source, target)
main_module.os.replace = failing_replace
try:
    assert not widget.commit_staged_output(stage, transaction, True)
finally:
    main_module.os.replace = replace
assert (transaction / "index.html").read_text(encoding="utf-8") == "old html"
assert (transaction / "asset/map.png").read_bytes() == b"old map"
assert (transaction / ".ringyo_zumen_outputs.json").read_bytes() == original_manifest
assert not list((transaction / "backup").rglob("*.json"))

# A collision with an unregistered user file is not overwritten or backed up.
(transaction / "manual.txt").write_text("user file", encoding="utf-8")
stage2 = output_root / "stage2"
stage2.mkdir()
(stage2 / "manual.txt").write_text("generated collision", encoding="utf-8")
assert not widget.commit_staged_output(stage2, transaction, True)
assert (transaction / "manual.txt").read_text(encoding="utf-8") == "user file"
assert (transaction / "index.html").read_text(encoding="utf-8") == "old html"
print("backup failure rollback and unregistered file collision protection: OK", flush=True)

# Without backup, discard only registered generated files and empty old directories.
cleanup = output_root / "cleanup"
cleanup.mkdir()
(cleanup / "old-drawing/shp").mkdir(parents=True)
(cleanup / "old-drawing/shp/old.shp").write_bytes(b"old shape")
(cleanup / "manual-folder").mkdir()
(cleanup / "manual-folder/user.txt").write_text("user file", encoding="utf-8")
(cleanup / "manual-folder/old.png").write_bytes(b"old generated map")
(cleanup / "index.html").write_text("old html", encoding="utf-8")
widget.write_output_manifest(cleanup / ".ringyo_zumen_outputs.json", [
    Path("old-drawing/shp/old.shp"), Path("manual-folder/old.png"), Path("index.html")])
cleanup_stage = output_root / "cleanup-stage"
cleanup_stage.mkdir()
(cleanup_stage / "index.html").write_text("new html", encoding="utf-8")
main_module.os.replace = lambda source, target: (
    failing_replace(stage / "index.html", target)
    if Path(source) == cleanup_stage / "index.html" else replace(source, target))
try:
    assert not widget.commit_staged_output(cleanup_stage, cleanup, False)
finally:
    main_module.os.replace = replace
assert (cleanup / "old-drawing/shp/old.shp").is_file()
assert (cleanup / "manual-folder/old.png").is_file()
assert (cleanup / "index.html").read_text(encoding="utf-8") == "old html"
assert widget.commit_staged_output(cleanup_stage, cleanup, False)
assert not (cleanup / "old-drawing").exists()
assert not (cleanup / "manual-folder/old.png").exists()
assert (cleanup / "manual-folder/user.txt").read_text(encoding="utf-8") == "user file"
assert json.loads((cleanup / ".ringyo_zumen_outputs.json").read_text(encoding="utf-8"))["paths"] == ["index.html"]
assert not list((cleanup / "backup").iterdir())
print("no-backup cleanup, unregistered files preserved, failure rollback: OK", flush=True)

# UI preview is real Qt, with both duplicated attribute panels visible in turn.
widget.resize(700, 930)
widget.show()
widget.tabWidget.setCurrentWidget(widget.tab_4)
app.processEvents()
widget.grab().save(str(output_root / "ancillary-ui.png"))
widget.close()
print("QA_OUTPUT=" + str(output_root), flush=True)
