"""Persist each exported layout with its own styles and editable label positions."""
from pathlib import Path
import shutil

from qgis.PyQt.QtXml import QDomDocument
from qgis.core import (
    Qgis, QgsAuxiliaryLayer, QgsLayoutItemMap, QgsLayoutItemPicture,
    QgsMapLayerStyle, QgsPalLayerSettings, QgsPrintLayout, QgsProject,
    QgsReadWriteContext, QgsReferencedRectangle, QgsVectorFileWriter,
    QgsVectorLayer,
)


class EditableProjects:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.drawings = []

    def capture(self, layout, name, output):
        """Freeze styles before the same source layers are restyled for another map."""
        document = QDomDocument()
        document.appendChild(layout.writeXml(document, QgsReadWriteContext()))
        layers = {}
        for item in layout.items():
            if isinstance(item, QgsLayoutItemMap):
                for layer in item.layers():
                    if layer.id() not in layers:
                        layers[layer.id()] = layer.clone()
        self.drawings.append((name, output, document.toString(), layers))

    def save(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        gpkg = self.directory / "ringyo_zumen.gpkg"
        sources = {}
        editable_ids = {
            layer_id for name, _, _, layers in self.drawings if name != "location"
            for layer_id in layers
        }
        # Write all data before opening any OGR readers (important on Windows).
        for _, _, _, layers in self.drawings:
            for original_id, layer in layers.items():
                if original_id in sources:
                    continue
                if isinstance(layer, QgsVectorLayer):
                    table = f"layer_{len(sources) + 1}"
                    options = QgsVectorFileWriter.SaveVectorOptions()
                    options.driverName = "GPKG"
                    options.layerName = table
                    options.fileEncoding = "UTF-8"
                    options.actionOnExistingFile = (
                        QgsVectorFileWriter.CreateOrOverwriteLayer if gpkg.exists()
                        else QgsVectorFileWriter.CreateOrOverwriteFile
                    )
                    result = QgsVectorFileWriter.writeAsVectorFormatV3(
                        layer, str(gpkg), QgsProject.instance().transformContext(), options)
                    if result[0] != QgsVectorFileWriter.NoError:
                        raise RuntimeError(f"GeoPackage保存失敗: {layer.name()}: {result[1]}")
                    sources[original_id] = (f"{gpkg}|layername={table}", "ogr")
                elif layer.providerType() == "gdal":
                    # Materialize local/VRT raster dependencies as a portable GeoTIFF.
                    from osgeo import gdal
                    target = self.directory / f"background_{len(sources) + 1}.tif"
                    dataset = gdal.Translate(str(target), layer.source(), format="GTiff",
                                             creationOptions=["COMPRESS=LZW"])
                    if dataset is None:
                        raise RuntimeError(f"背景地図の保存失敗: {layer.name()}")
                    dataset = None
                    sources[original_id] = (str(target), "gdal")
                else:
                    # XYZ/WMS retain their connection and render again when reopened.
                    sources[original_id] = (layer.source(), layer.providerType())

        for name, output, xml, layers in self.drawings:
            project = QgsProject()
            project.setFileName(str(self.directory / f"{name}.qgz"))
            project.setFilePathStorage(Qgis.FilePathType.Relative)
            project.setTransformContext(QgsProject.instance().transformContext())
            project.setLabelingEngineSettings(QgsProject.instance().labelingEngineSettings())
            for original_id, layer in layers.items():
                style = QgsMapLayerStyle()
                style.readFromLayer(layer)
                uri, provider = sources[original_id]
                layer.setDataSource(uri, layer.name(), provider)
                if not layer.isValid():
                    raise RuntimeError(f"保存レイヤを開けません: {layer.name()}")
                style.writeToLayer(layer)
                project.addMapLayer(layer)
                xml = xml.replace(original_id, layer.id())
                if original_id in editable_ids and isinstance(layer, QgsVectorLayer) and layer.labelsEnabled():
                    auxiliary = project.auxiliaryStorage().createAuxiliaryLayer(
                        layer.fields().field("fid"), layer)
                    if auxiliary is None:
                        raise RuntimeError(f"ラベル位置の保存領域を作成できません: {layer.name()}")
                    layer.setAuxiliaryLayer(auxiliary)
                    for prop in (QgsPalLayerSettings.PositionX, QgsPalLayerSettings.PositionY):
                        if QgsAuxiliaryLayer.createProperty(prop, layer) < 0:
                            raise RuntimeError(f"ラベル移動の設定に失敗しました: {layer.name()}")

            document = QDomDocument()
            document.setContent(xml)
            layout = QgsPrintLayout(project)
            layout.loadFromTemplate(document, QgsReadWriteContext())
            layout.setName(name)
            layout.renderContext().setDpi(300)
            for item in layout.items():
                if isinstance(item, QgsLayoutItemMap):
                    item.setKeepLayerSet(True)
                    item.setKeepLayerStyles(False)
                    project.setCrs(item.crs())
                    project.viewSettings().setDefaultViewExtent(
                        QgsReferencedRectangle(item.extent(), item.crs()))
                    project.viewSettings().setDefaultRotation(item.mapRotation())
                    project.layerTreeRoot().setHasCustomLayerOrder(True)
                    project.layerTreeRoot().setCustomLayerOrder(item.layers())
                elif isinstance(item, QgsLayoutItemPicture) and item.picturePath():
                    source = Path(item.picturePath())
                    if source.is_file():
                        target = self.directory / source.name
                        shutil.copy2(source, target)
                        item.setPicturePath(str(target))
            project.layoutManager().addLayout(layout)
            if not project.write():
                raise RuntimeError(f"QGZ保存失敗: {name}")
            project.clear()
        instructions = Path(__file__).parent / "qgs_editing.md"
        text = instructions.read_text(encoding="utf-8")
        text += "\n## 今回出力した図面\n\n| QGZ | レイアウト | 書き出し先（出力フォルダ基準） |\n|---|---|---|\n"
        for name, output, _, _ in self.drawings:
            text += f"| `{name}.qgz` | `{name}` | `{output.replace('|', '&#124;')}` |\n"
        (self.directory / "操作説明.md").write_text(text, encoding="utf-8")
        self.drawings.clear()
