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
QA_ROOT = Path(os.environ.get("QGIS_TEST_OUTPUT_DIR", tempfile.gettempdir()))
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
assert not hasattr(widget, "rinshohan")
widget.shinseibango.setText("123-01")
widget.seizusha2.setText("製図者")
widget.sanrinshoyusha2.setText("所有者")
widget.seizubi2.setDate(QDate(2026, 10, 5))
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
    assert button.isEnabled() == (field not in ("hukuin", "singleLineWidth", "minKijuntenkan")), field
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
assert not hasattr(widget, "minKijuntenkanOverride")
widget.minJochi.setValue(1)
widget.isJochikeisan.setChecked(True)
widget.isIchizu.setChecked(True)
widget.locationPaper.setCurrentText("A3")
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

# The application header uses resolved feature attributes, with duplicates and NULLs omitted.
assert widget.drawing_metadata()["name"] == "12林班60小班"
widget.shohanOverride.setToProperty(QgsProperty.fromExpression("if(\"name\" = '区域A', 60, 61)"))
widget.calculate_uav()
assert widget.drawing_metadata()["name"] == "12林班60小班、12林班61小班"
assert widget.ortho_file_stem() == "12林班60・61小班 - オルソ"
assert widget.project_file_name() == "12林班60・61小班.qgz"
area_records = [
    {"市町村": city, "林班": rinpan, "小班": shohan}
    for city, rinpan, shohan in (("A町", 12, 61), ("A町", 13, 2), ("B市", 5, 3),
                               ("A町", 12, 60), ("A町", 12, 61))]
expected_area = "5林班3小班_12林班60・61小班_13林班2小班"
assert widget.ortho_area_name(area_records) == expected_area
assert widget.ortho_area_name(reversed(area_records)) == expected_area
assert widget.ortho_area_name([{"市町村": None, "林班": 0, "小班": None}]) == "0林班"
assert widget.ortho_area_name([{"市町村": "A町", "林班": None, "小班": 2}]) == "2小班"
assert widget.ortho_area_name([{"市町村": "A町", "林班": 12, "小班": 60},
                               {"市町村": "B市", "林班": 12, "小班": 60}]) == "12林班60小班"
assert widget.ortho_area_name([{"市町村": None, "林班": None, "小班": None}]) == ""
assert len(widget.safe_file_name("市" * 130, max_length=None)) == 130
header_layer = widget.application_layer
rinpan_index = header_layer.fields().indexFromName("林班")
shohan_index = header_layer.fields().indexFromName("小班")
assert header_layer.dataProvider().changeAttributeValues({feature.id(): {rinpan_index: None}
                                                        for feature in header_layer.getFeatures()})
assert widget.drawing_metadata()["name"] == "60小班、61小班"
assert header_layer.dataProvider().changeAttributeValues({feature.id(): {shohan_index: None}
                                                        for feature in header_layer.getFeatures()})
assert widget.drawing_metadata()["name"] == ""
all_drawings = widget.drawings
widget.drawings = [widget.drawings[0]]
assert widget.project_file_name() == "未指定.qgz"
assert widget.ortho_file_stem() == "オルソ"
widget.drawings = all_drawings
for field in ("rinpan", "shohan"):
    getattr(widget, field + "Override").setToProperty(QgsProperty())
    widget.update_override_enabled(field)
widget.calculate_uav()
print("application header: duplicate compartments, per-feature overrides and NULLs: OK", flush=True)

widget.activate_drawing(widget.drawings[1])
assert widget.drawing_metadata()["name"] == "12林班60小班 道A"
assert widget.compartment_name({"林班": 0, "小班": None, "枝番": "1"}, True) == "0林班（枝番1）"
assert widget.compartment_name({"林班": None, "小班": None, "枝番": None}, True) == ""
widget.activate_drawing(widget.drawings[0])

# Headers include every application value and never borrow from the other tab.
header_config = widget.configuration_data()
for field in ("seizujigyosha", "seizusha2", "sanrinshoyusha2", "shinseibango"):
    getattr(widget, field + "Override").setToProperty(QgsProperty.fromExpression("'施行地' || \"name\""))
widget.seizubi2Override.setToProperty(QgsProperty.fromExpression("if(\"name\" = '区域A', to_date('2026-10-05'), to_date('2026-10-06'))"))
widget.calculate_uav()
app_header = widget.drawing_metadata()
for field in ("company", "draftsperson", "owner", "application_no"):
    assert app_header[field] == "施行地区域A、施行地区域B", (field, app_header)
assert app_header["date"] == "2026年10月05日、2026年10月06日"
widget.activate_drawing(widget.drawings[1])
line_header = widget.drawing_metadata()
assert line_header["company"] == ""
assert line_header["application_no"] == ""
assert "施行地" not in line_header["draftsperson"] and "施行地" not in line_header["owner"]
assert line_header["date"] == "2026年10月01日"
attributes = widget._current_drawing["attributes"]
del attributes["製図事業者"]
assert widget.drawing_metadata()["company"] == ""
widget.singleLineSeizujigyosha.setText("付帯のみの会社")
widget.singleLineShinseibango.setText("付帯のみの番号")
widget.calculate_uav()
assert widget.drawing_metadata() == app_header
widget.apply_configuration(header_config)
widget.calculate_uav()
print("headers: all unique application values, blank ancillary values and independent tabs OK", flush=True)

data = widget.configuration_data()
assert "basic" not in data
json.dumps(data)
restored = Main()
assert not restored.apply_configuration(data)
assert "basic" not in restored.configuration_data()
assert restored.singleLineWidthOverride.toProperty().field() == "width"
assert restored.singleLine.currentLayer() is single_lines
assert restored.minKijuntenkan.value() == 20
assert restored.paper.currentText() == "A4" and restored.locationPaper.currentText() == "A3"
restored.close()

# Every ancillary attribute supports explicit inheritance through its override menu.
from RingyoZumenMaker.uav_inputs import ATTRIBUTE_INPUTS, prefixed
line_feature = next(single_lines.getFeatures())
widget.seizujigyosha.setText("施行地事業者")
widget.shichosonOverride.setToProperty(QgsProperty.fromExpression("'市町村' || \"name\""))
for field, alias in ATTRIBUTE_INPUTS:
    name = prefixed("singleLine", field)
    button = getattr(widget, name + "Override")
    button.menu().aboutToShow.emit()
    action = next(a for a in button.menu().actions() if a.text() == "施行地の値を継承")
    assert not action.isChecked()
    action.trigger()
    assert button.isActive() and not getattr(widget, name).isEnabled(), name
    assert widget.override_value(name, single_lines, line_feature) == widget.application_attribute_value(field), name
inherited = widget.polygon_attributes(single_lines, line_feature, "道A", "singleLine")
assert inherited["市町村"] == "市町村区域A"
assert inherited["申請No"] == widget.shinseibango.text()
widget.calculate_uav()
widget.activate_drawing(widget.drawings[1])
assert next(widget.application_layer.getFeatures())["市町村"] == "市町村区域A"
assert widget.drawing_metadata()["company"] == "施行地事業者"
assert widget.drawing_metadata()["application_no"] == inherited["申請No"]
inherit_config = widget.configuration_data()
restored = Main()
assert not restored.apply_configuration(inherit_config)
restored.validate_inputs()
assert restored.polygon_attributes(single_lines, line_feature, "道A", "singleLine") == inherited
restored.close()
widget.singleLineShichoson.setText("")
button = widget.singleLineShichosonOverride
button.menu().aboutToShow.emit()
action = next(a for a in button.menu().actions() if a.text() == "施行地の値を継承")
assert action.isChecked()
action.trigger()
assert not button.isActive() and widget.singleLineShichoson.isEnabled()
assert widget.override_value("singleLineShichoson", single_lines, line_feature) == ""
widget.polygon.setLayer(None)
assert widget.application_attribute_value("shichoson") == widget.shichoson.text()
widget.edaban.setExpression("'8'")
assert widget.application_attribute_value("edaban") == "8"
widget.apply_configuration(data)
widget.calculate_uav()
print("explicit menu inheritance: all attributes, first polygon expression, empty local values and config round-trip: OK", flush=True)
widget.on_submit(test=True)
assert widget.progressBar.value() == 100, messages
assert not list(output.iterdir())
widget.on_submit(test=False)
assert widget.progressBar.value() == 100, messages
assert (output / "index.html").is_file()
assert (output / "backup").is_dir()
assert not (output / "共通").exists()
assert not (output / "asset/QGISで図面を編集する方法.md").exists()
assert not list(output.rglob("*.zip"))
assert len(list(output.rglob("shp/*.shp"))) == 3
assert len(list(output.glob("qgz/*.qgz"))) == 1
assert (output / "位置図/位置図.pdf").is_file()
assert len(list(output.rglob("ringyo_zumen.gpkg"))) == 1
assert (output / "qgz/ringyo_zumen.gpkg").is_file()
assert (output / "オルソ/12林班60小班 - オルソ.tif").is_file()
html = ET.parse(str(output / "index.html"), ET.HTMLParser())
assert len(html.xpath("//div[@class='drawing-sheet main_container']")) == 3
assert html.xpath("//div[@data-drawing='1']//div[contains(@id, 'drawing_name')]/span/text()") == ["12林班60小班"]
assert html.xpath("//div[@data-drawing='2']//div[contains(@id, 'drawing_name')]/span/text()") == ["12林班60小班 道A"]
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
    assert project.read(str(output / "qgz" / widget.project_file_name()))
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
    assert project.read(str(output / "qgz" / widget.project_file_name()))
    layout = project.layoutManager().layoutByName("位置図")
    page = layout.pageCollection().page(0).pageSize()
    assert (page.width(), page.height()) == (297, 420)
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

# Backup named output folders, including manually added files, without an index.
previous = widget.output_target_paths(output)
assert not (output / ".ringyo_zumen_outputs.json").exists()
manual = output / widget.drawings[1]["folder"] / "user-note.txt"
manual.write_text("手動追加", encoding="utf-8")
widget.singleLineName.setExpression("'新名称' || \"name\"")
widget.singleLineFilter.setExpression('"width" = 2')
widget.backupQgz.setChecked(True)
widget.on_submit(test=False)
assert widget.progressBar.value() == 100, messages
batch = next((output / "backup").iterdir())
assert all((batch / relative).is_file() for relative in previous)
assert not manual.exists()
assert (batch / manual.relative_to(output)).read_text(encoding="utf-8") == "手動追加"
assert len([path for path in output.rglob("shp/*.shp") if "backup" not in path.relative_to(output).parts]) == 2
assert not (batch / ".ringyo_zumen_outputs.json").exists()
assert not (output / ".ringyo_zumen_outputs.json").exists()
print("backup: named folders, renamed drawings and user additions, no output index: OK", flush=True)

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
assert len(sheet.xpath(".//div[contains(concat(' ', normalize-space(@class), ' '), ' calc-result ')]")) == 1
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
assert (line_only_output / "qgz" / widget.project_file_name()).is_file()
line_project = QgsProject()
assert line_project.read(str(line_only_output / "qgz" / widget.project_file_name()))
assert all(layer.isValid() for layer in line_project.mapLayers().values())
line_project.clear()
print("horizontal line only, no orthophoto or reference points: exports OK", flush=True)

# Unsupported config versions fail before changing the current widget values.
current_config = widget.configuration_data()
for version in (None, 3, 4, 5, 7):
    unsupported = json.loads(json.dumps(current_config))
    unsupported["version"] = version
    try:
        widget.apply_configuration(unsupported)
        raise AssertionError("Unsupported version was accepted")
    except ValueError:
        pass
    assert widget.configuration_data() == current_config
print("current config round-trip; unsupported versions rejected without changing inputs: OK", flush=True)

# A failed commit restores the named files.
from RingyoZumenMaker import main as main_module
transaction = output_root / "transaction"
transaction.mkdir()
(transaction / "asset").mkdir()
(transaction / "index.html").write_text("old html", encoding="utf-8")
(transaction / "asset/map.png").write_bytes(b"old map")
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

# Without backup, remove obsolete files only inside named folders; ignore other configs.
cleanup = output_root / "cleanup"
cleanup.mkdir()
(cleanup / "付帯作工物/旧図面/shp").mkdir(parents=True)
(cleanup / "付帯作工物/旧図面/shp/old.shp").write_bytes(b"old shape")
(cleanup / "manual-folder").mkdir()
(cleanup / "manual-folder/user.txt").write_text("user file", encoding="utf-8")
(cleanup / "asset").mkdir()
(cleanup / "asset/old.png").write_bytes(b"old map")
(cleanup / "asset/custom.config").write_text("nested config", encoding="utf-8")
(cleanup / "custom.config").write_text("custom config", encoding="utf-8")
(cleanup / "index.html").write_text("old html", encoding="utf-8")
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
assert (cleanup / "付帯作工物/旧図面/shp/old.shp").is_file()
assert (cleanup / "asset/old.png").is_file()
assert (cleanup / "index.html").read_text(encoding="utf-8") == "old html"
assert widget.commit_staged_output(cleanup_stage, cleanup, False)
assert not (cleanup / "付帯作工物/旧図面").exists()
assert not (cleanup / "asset/old.png").exists()
assert (cleanup / "manual-folder/user.txt").read_text(encoding="utf-8") == "user file"
assert not (cleanup / ".ringyo_zumen_outputs.json").exists()
assert (cleanup / "custom.config").read_text(encoding="utf-8") == "custom config"
assert (cleanup / "asset/custom.config").read_text(encoding="utf-8") == "nested config"
assert not list((cleanup / "backup").iterdir())
print("no-backup named-folder cleanup, other configs/files preserved, failure rollback: OK", flush=True)

# No export history is needed; backup selection is entirely by direct names.
named = output_root / "named-targets"
named.mkdir()
for relative in ("index.html", "input.config", "asset/manual.txt", "qgz/custom.qgz",
                 "位置図/任意.pdf", "オルソ/任意.tif", "申請区域 - 全体/shp/任意.dbf",
                 "付帯作工物/任意/shp/任意.shp", "付帯作工物 - 旧名称/任意.txt",
                 "asset/other.config", "custom.config", "input-copy.config",
                 "other-folder/index.html", "backup/past/keep.txt"):
    item = named / relative
    item.parent.mkdir(parents=True, exist_ok=True)
    item.write_text(relative, encoding="utf-8")
named_stage = output_root / "named-stage"
named_stage.mkdir()
(named_stage / "index.html").write_text("new", encoding="utf-8")
assert widget.commit_staged_output(named_stage, named, True)
named_batch = next(p for p in (named / "backup").iterdir() if p.name != "past")
for relative in ("index.html", "input.config", "asset/manual.txt", "qgz/custom.qgz",
                 "位置図/任意.pdf", "オルソ/任意.tif", "申請区域 - 全体/shp/任意.dbf",
                 "付帯作工物/任意/shp/任意.shp"):
    assert (named_batch / relative).read_text(encoding="utf-8") == relative
for relative in ("asset/other.config", "custom.config", "input-copy.config", "other-folder/index.html", "backup/past/keep.txt", "付帯作工物 - 旧名称/任意.txt"):
    assert (named / relative).read_text(encoding="utf-8") == relative
    assert not (named_batch / relative).exists()
assert not (named / ".ringyo_zumen_outputs.json").exists()
assert not (named_batch / ".ringyo_zumen_outputs.json").exists()
print("named backup: no history, arbitrary contents, root input.config only, outside folders ignored OK", flush=True)

# UI preview is real Qt, with both duplicated attribute panels visible in turn.
widget.resize(700, 930)
widget.show()
widget.tabWidget.setCurrentWidget(widget.tab_4)
app.processEvents()
widget.grab().save(str(output_root / "ancillary-ui.png"))
widget.close()
print("QA_OUTPUT=" + str(output_root), flush=True)
