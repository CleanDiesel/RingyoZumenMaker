"""Verify editable project regeneration and recalculation without project output."""
import runpy
from pathlib import Path

fixture = runpy.run_path(str(Path(__file__).with_name("uav_multidrawing.py")))
globals().update({key: value for key, value in fixture.items() if not key.startswith("__")})
from RingyoZumenMaker.unified_project import metadata, UID
from qgis.core import QgsLayoutItemMap, QgsLayoutItemLabel, QgsLayoutPoint, QgsMapLayerStyle
from qgis.PyQt.QtGui import QColor
import hashlib
import sqlite3

project = QgsProject.instance()
project.clear()
qgz = output / "qgz" / widget.project_file_name()
assert project.read(str(qgz))
widget.singleLineName.setExpression('"name"')
assert widget.fileName.filePath() == str(output.resolve())
assert widget.singleLine.currentLayer().featureCount() == 2, "Filtered-out inputs must still be saved"
assert widget.singleLineFilter.expression() == '"width" = 2'
info = metadata(project)
aggregate = project.mapLayer(info["datasets"]["output:aggregate"])
aggregate.styleManager().setCurrentStyle("図面:aggregate")
from qgis.core import QgsExpression, QgsExpressionContext, QgsExpressionContextUtils, QgsGeometryGeneratorSymbolLayer
generator = next(symbol for symbol in aggregate.renderer().symbol().symbolLayers() if isinstance(symbol, QgsGeometryGeneratorSymbolLayer))
expression = QgsExpression(generator.geometryExpression())
context = QgsExpressionContext()
context.appendScopes(QgsExpressionContextUtils.globalProjectLayerScopes(aggregate))
context.setFeature(next(aggregate.getFeatures()))
evaluated = expression.evaluate(context)
assert isinstance(evaluated, QgsGeometry) and abs(evaluated.area() - 350) < 1e-6, (expression.expression(), evaluated, expression.evalErrorString())
line = next(item for item in info["layouts"] if item["key"].startswith("line:"))
layout = project.layoutManager().layoutByName(line["layout"])
map_item = next(item for item in layout.items() if isinstance(item, QgsLayoutItemMap))
map_item.setScale(3456)
map_item.setMapRotation(12)
extent = map_item.extent().toString(8)
label = QgsLayoutItemLabel(layout)
label.setText("手動編集の文字")
label.attemptMove(QgsLayoutPoint(3, 3))
layout.addLayoutItem(label)
project.setTitle("未保存のタイトル")
out_layer = project.mapLayer(info["datasets"]["output:" + line["key"]])
out_layer.styleManager().setCurrentStyle("図面:" + line["key"])
out_layer.renderer().symbol().setColor(QColor("green"))
aux = out_layer.auxiliaryLayer()
assert aux is not None
aux.startEditing()
target = next(out_layer.getFeatures())
target["auxiliary_storage_labeling_label_position_x"] = 30
target["auxiliary_storage_labeling_label_position_y"] = 40
out_layer.startEditing()
assert out_layer.changeAttributeValue(target.id(), out_layer.fields().indexFromName("auxiliary_storage_labeling_label_position_x"), 30)
assert out_layer.changeAttributeValue(target.id(), out_layer.fields().indexFromName("auxiliary_storage_labeling_label_position_y"), 40)
assert out_layer.commitChanges()
assert aux.commitChanges()
# A user-created layout and external vector must survive.
from qgis.core import QgsPrintLayout
user_layout = QgsPrintLayout(project)
user_layout.initializeDefaults()
user_layout.setName("自分のレイアウト")
project.layoutManager().addLayout(user_layout)
user_scratch = vector("追加スクラッチ", "Point", [("残す", "POINT(25 25)", 1)])
source_layer = widget.singleLine.currentLayer()
source_layer.startEditing()
source_layer.changeAttributeValue(next(source_layer.getFeatures()).id(), source_layer.fields().indexFromName("name"), "名称変更")
assert source_layer.commitChanges()
widget.backupQgz.setChecked(True)
old_qgz = qgz
for prefix in ("", "singleLine"):
    getattr(widget, "rinpan" if not prefix else "singleLineRinpan").setValue(13)
    getattr(widget, "shohan" if not prefix else "singleLineShohan").setValue(61)
widget.on_submit(test=False)
qgz = output / "qgz" / widget.project_file_name()
assert qgz.name == "13林班61小班.qgz"
assert qgz != old_qgz and not old_qgz.exists()
assert len(list((output / "qgz").glob("*.qgz"))) == 1
assert widget.progressBar.value() == 100, messages
assert Path(project.fileName()).resolve() == qgz.resolve(), project.fileName()
new_info = metadata(project)
new_line = next(item for item in new_info["layouts"] if item["key"] == line["key"])
new_layout = project.layoutManager().layoutByName(new_line["layout"])
new_map = next(item for item in new_layout.items() if isinstance(item, QgsLayoutItemMap))
assert abs(new_map.scale() - 3456) < 0.001
assert new_map.mapRotation() == 12
assert new_map.extent().toString(8) == extent
assert any(isinstance(item, QgsLayoutItemLabel) and item.text() == "手動編集の文字" for item in new_layout.items())
assert project.layoutManager().layoutByName("自分のレイアウト") is not None
new_layer = project.mapLayer(new_info["datasets"]["output:" + line["key"]])
new_layer.styleManager().setCurrentStyle("図面:" + line["key"])
assert new_layer.renderer().symbol().color() == QColor("green")
assert next(new_layer.getFeatures())["label"] == "名称変更"
assert project.title() == "未保存のタイトル"
for key, layer_id in new_info["datasets"].items():
    layer = project.mapLayer(layer_id)
    if hasattr(layer, "getFeatures"):
        assert layer.featureCount() == len(list(layer.getFeatures())) > 0, (key, layer.source(), layer.featureCount())
saved_scratch = next(layer for layer in project.mapLayers().values() if layer.name() == "追加スクラッチ")
assert saved_scratch.isValid() and saved_scratch.featureCount() == 1
assert next(new_layer.getFeatures())["auxiliary_storage_labeling_label_position_x"] == 30
latest_backup = max((output / "backup").iterdir(), key=lambda path: path.stat().st_mtime_ns)
backup_project = QgsProject()
assert backup_project.read(str(latest_backup / "qgz" / old_qgz.name))
assert backup_project.title() == "未保存のタイトル"
assert all(layer.isValid() for layer in backup_project.mapLayers().values())
backup_project.clear()
with sqlite3.connect(str(output / "qgz/ringyo_zumen.gpkg")) as connection:
    tables = [row[0] for row in connection.execute("SELECT table_name FROM gpkg_contents")]
assert len(tables) == len(set(tables)) == len(new_info["tables"])
assert all(project.mapLayer(layer_id).isValid() for layer_id in new_info["datasets"].values())
print("same-folder regeneration: stable IDs, manual layouts/styles/extent, current-state backup OK", flush=True)

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

protected = [qgz, output / "qgz/ringyo_zumen.gpkg"]
before = [digest(path) for path in protected]
new_layer.startEditing()
assert new_layer.changeAttributeValue(next(new_layer.getFeatures()).id(), new_layer.fields().indexFromName("幅m"), 2.34)
source_layer = widget.singleLine.currentLayer()
source_layer.startEditing()
source_feature = next(f for f in source_layer.getFeatures() if f["width"] == 2)
assert source_layer.changeGeometry(source_feature.id(), QgsGeometry.fromWkt("MULTILINESTRING((0 10,240 10))"))
widget.singleLineWidthOverride.setToProperty(QgsProperty.fromExpression("3.45"))
before = [digest(path) for path in protected]
project_name = project.fileName()
map_extent = new_map.extent().toString(8)
questions = QMessageBox.question
QMessageBox.question = lambda *_: (_ for _ in ()).throw(AssertionError("No project output must not save input edits"))
widget.outputMode.setCurrentIndex(1)
try:
    widget.on_submit(test=False)
finally:
    QMessageBox.question = questions
assert widget.progressBar.value() == 100, messages
assert [digest(path) for path in protected] == before
assert "3.45" in (output / "index.html").read_text(encoding="utf-8")
assert "240 m" in (output / "index.html").read_text(encoding="utf-8")
assert project.fileName() == project_name and new_map.extent().toString(8) == map_extent
assert source_layer.isModified()
assert new_layer.isModified()
assert source_layer.rollBack()
assert new_layer.rollBack()
widget.singleLineWidthOverride.setToProperty(QgsProperty.fromField("width"))
latest_backup = max((output / "backup").iterdir(), key=lambda path: path.stat().st_mtime_ns)
assert not list(latest_backup.rglob("*.qgz")) and not list(latest_backup.rglob("*.gpkg"))
assert all(path.is_file() for path in protected)
assert not (output / ".ringyo_zumen_outputs.json").exists()
print("no QGZ: input geometry/width recalculated, HTML/SHP/PDF replaced, QGZ/GPKG and open project untouched OK", flush=True)

# A new output folder receives no project or package, and the selection round-trips.
fresh_output = output_root / "no-project-output"
fresh_output.mkdir()
widget.fileName.setFilePath(str(fresh_output))
widget.backupQgz.setChecked(False)
widget.on_submit(test=False)
assert widget.progressBar.value() == 100, messages
assert not (fresh_output / "qgz").exists()
assert not list(fresh_output.rglob("*.qgz")) and not list(fresh_output.rglob("*.gpkg"))
assert (fresh_output / "index.html").is_file() and (fresh_output / "位置図/位置図.pdf").is_file()
assert list(fresh_output.rglob("shp/*.shp"))
assert widget.configuration_data()["output"]["create_qgz"] is False
restored = Main()
restored.apply_configuration(widget.configuration_data())
assert restored.outputMode.currentIndex() == 1
restored.close()
widget.fileName.setFilePath(str(output))
# The no-backup path must also preserve previously generated packages.
widget.on_submit(test=False)
assert widget.progressBar.value() == 100, messages
assert [digest(path) for path in protected] == before
widget.backupQgz.setChecked(True)
print("no QGZ: fresh folder, configuration round-trip and existing files preserved without backup OK", flush=True)

source_layer = widget.singleLine.currentLayer()
source_layer.startEditing()
source_layer.changeAttributeValue(next(source_layer.getFeatures()).id(), source_layer.fields().indexFromName("name"), "未保存の変更")
before = [digest(path) for path in protected]
question = QMessageBox.question
QMessageBox.question = lambda *_: QMessageBox.StandardButton.Cancel
widget.outputMode.setCurrentIndex(0)
try:
    widget.on_submit(test=False)
finally:
    QMessageBox.question = question
assert [digest(path) for path in protected] == before
assert source_layer.isModified()
source_layer.rollBack()
print("unsaved geometry edit cancel: files and current edit buffer retained OK", flush=True)

# Background files remain external; background scratch layers are not drawn.
background_memory = vector("外部背景", "MultiPolygon", [("背景", "MULTIPOLYGON(((0 0,300 0,300 200,0 200,0 0)))", 1)])
from qgis.core import QgsVectorFileWriter
background_path = output_root / "background.gpkg"
options = QgsVectorFileWriter.SaveVectorOptions()
options.driverName = "GPKG"
options.layerName = "background"
assert QgsVectorFileWriter.writeAsVectorFormatV3(background_memory, str(background_path), project.transformContext(), options)[0] == QgsVectorFileWriter.NoError
background = QgsVectorLayer(str(background_path) + "|layername=background", "外部背景", "ogr")
project.addMapLayer(background)
class Canvas:
    def layers(self):
        return [background, background_memory]
class Iface:
    def mapCanvas(self):
        return Canvas()
widget.iface = Iface()
widget.outputMode.setCurrentIndex(0)
background_output = output_root / "background-output"
background_output.mkdir()
widget.fileName.setFilePath(str(background_output))
widget.on_submit(test=False)
assert widget.progressBar.value() == 100, messages
background_project = QgsProject()
assert background_project.read(str(background_output / "qgz" / widget.project_file_name()))
background_info = metadata(background_project)
background_key = next(key for key in background_info["datasets"] if key.startswith("background:"))
persisted_background = background_project.mapLayer(background_info["datasets"][background_key])
assert Path(persisted_background.source().split("|")[0]).resolve() == background_path.resolve()
location = background_project.layoutManager().layoutByName("位置図")
location_map = location.itemById("地図 1")
assert persisted_background in location_map.layersToRender()
assert all(layer.providerType() != "memory" and layer.providerType() != "gdal" for layer in location_map.layersToRender())
assert all(background_project.mapLayer(layer_id).featureCount() for key, layer_id in background_info["datasets"].items()
           if key.startswith("input:") and hasattr(background_project.mapLayer(layer_id), "featureCount"))
background_project.clear()
widget.iface = None
print("location background: external references retained, scratch and ortho excluded OK", flush=True)
print("UNIFIED_QA=" + str(output), flush=True)
