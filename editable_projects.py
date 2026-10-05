"""Persist each exported layout with its own styles and editable label positions."""
from pathlib import Path
import os
import re
import shutil
import tempfile
import zipfile

from qgis.PyQt.QtXml import QDomDocument
from qgis.core import (
    Qgis, QgsAuxiliaryLayer, QgsLayoutItemMap, QgsLayoutItemPicture,
    QgsMapLayerStyle, QgsPalLayerSettings, QgsPrintLayout, QgsProject,
    QgsProperty, QgsPropertyDefinition, QgsReadWriteContext,
    QgsReferencedRectangle, QgsVectorFileWriter,
    QgsVectorLayer,
)


class EditableProjects:
    def __init__(self, directory, shared_files=(), shared_directory=None, location_directory=None, gpkg_directory=None):
        self.directory = Path(directory)
        self.shared_directory = Path(shared_directory) if shared_directory is not None else self.directory
        self.location_directory = Path(location_directory) if location_directory is not None else self.shared_directory
        self.gpkg_directory = Path(gpkg_directory) if gpkg_directory is not None else self.shared_directory
        self.shared_files = tuple(Path(path) for path in shared_files if path)
        self.drawings = []
        self.persistent_layer_ids = set()

    def capture(self, layout, name, output, persistent_layers=()):
        """Freeze styles before the same source layers are restyled for another map."""
        persistent_ids = {
            layer.id() for layer in persistent_layers if layer is not None
        }

        # Application drawings persist generated vectors, not raster backgrounds.
        if name != "location":
            for item in layout.items():
                if isinstance(item, QgsLayoutItemMap):
                    persistent_ids.update(
                        layer.id()
                        for layer in item.layers()
                        if isinstance(layer, QgsVectorLayer)
                    )
        self.persistent_layer_ids.update(persistent_ids)

        layers = {}
        for item in layout.items():
            if isinstance(item, QgsLayoutItemMap):
                kept_layers = list(item.layers())
                item.setLayers(kept_layers)
                for layer in kept_layers:
                    if layer.id() not in layers:
                        layers[layer.id()] = layer.clone()

        # 除去前・採用除地のような非表示の成果レイヤもGeoPackageへ保存する。
        for layer in persistent_layers:
            if layer is not None and layer.id() not in layers:
                layers[layer.id()] = layer.clone()

        document = QDomDocument()
        document.appendChild(layout.writeXml(document, QgsReadWriteContext()))
        self.drawings.append((name, output, document.toString(), layers))

    def save(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.shared_directory.mkdir(parents=True, exist_ok=True)
        self.gpkg_directory.mkdir(parents=True, exist_ok=True)
        gpkg = self.gpkg_directory / "ringyo_zumen.gpkg"
        if gpkg.exists():
            gpkg.unlink()
        sources = {}
        editable_ids = set(self.persistent_layer_ids)
        # Write all data before opening any OGR readers (important on Windows).
        for _, _, _, layers in self.drawings:
            for original_id, layer in layers.items():
                if original_id in sources or original_id not in editable_ids:
                    continue
                if not isinstance(layer, QgsVectorLayer):
                    raise RuntimeError(f"GeoPackageへ保存できないレイヤです: {layer.name()}")
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

        for name, output, xml, layers in self.drawings:
            project = QgsProject()
            project_path = (self.location_directory if name == "location" else self.directory) / f"{name}.qgz"
            project_path.parent.mkdir(parents=True, exist_ok=True)
            project.setFileName(str(project_path))
            # 外部背景は絶対パス。共有成果だけを保存後に相対化する。
            project.setFilePathStorage(Qgis.FilePathType.Absolute)
            project.setTransformContext(QgsProject.instance().transformContext())
            labeling_settings = QgsProject.instance().labelingEngineSettings()
            labeling_settings.setFlag(Qgis.LabelingFlag.UsePartialCandidates, True)
            project.setLabelingEngineSettings(labeling_settings)
            for layer_index, (original_id, layer) in enumerate(layers.items(), start=1):
                style = QgsMapLayerStyle()
                style.readFromLayer(layer)
                if original_id in sources:
                    uri, provider = sources[original_id]
                    layer.setDataSource(uri, layer.name(), provider)
                    if not layer.isValid():
                        raise RuntimeError(f"保存レイヤを開けません: {layer.name()}")
                elif isinstance(layer, QgsVectorLayer) and layer.providerType() == "memory":
                    # A scratch layer has no durable source. Store its data as a
                    # QGZ attachment without adding it to ringyo_zumen.gpkg.
                    table = f"scratch_{layer_index}"
                    attachment = project.createAttachedFile(f"{table}.gpkg")
                    options = QgsVectorFileWriter.SaveVectorOptions()
                    options.driverName = "GPKG"
                    options.layerName = table
                    options.fileEncoding = "UTF-8"
                    options.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteFile
                    result = QgsVectorFileWriter.writeAsVectorFormatV3(
                        layer,
                        attachment,
                        QgsProject.instance().transformContext(),
                        options,
                    )
                    if result[0] != QgsVectorFileWriter.NoError:
                        raise RuntimeError(
                            f"一時スクラッチレイヤ保存失敗: {layer.name()}: {result[1]}"
                        )
                    layer.setDataSource(
                        f"{attachment}|layername={table}", layer.name(), "ogr"
                    )
                    if not layer.isValid():
                        raise RuntimeError(
                            f"保存した一時スクラッチレイヤを開けません: {layer.name()}"
                        )
                style.writeToLayer(layer)
                project.addMapLayer(layer)
                xml = xml.replace(original_id, layer.id())
                if original_id in editable_ids and isinstance(layer, QgsVectorLayer) and layer.labelsEnabled():
                    auxiliary = project.auxiliaryStorage().createAuxiliaryLayer(
                        layer.fields().field("fid"), layer)
                    if auxiliary is None:
                        raise RuntimeError(f"ラベル位置の保存領域を作成できません: {layer.name()}")
                    layer.setAuxiliaryLayer(auxiliary)
                    labeling = layer.labeling()
                    provider_ids = labeling.subProviders() or [""]
                    for provider_index, provider_id in enumerate(provider_ids, start=1):
                        settings = labeling.settings(provider_id)
                        properties = settings.dataDefinedProperties()
                        for prop, axis in (
                            (QgsPalLayerSettings.PositionX, "x"),
                            (QgsPalLayerSettings.PositionY, "y"),
                        ):
                            definition = QgsPropertyDefinition(
                                f"label_{provider_index}_position_{axis}",
                                f"Label {provider_index} position {axis.upper()}",
                                QgsPropertyDefinition.StandardPropertyTemplate.Double,
                                "labeling",
                            )
                            if not auxiliary.addAuxiliaryField(definition):
                                raise RuntimeError(
                                    f"ラベル移動の保存項目を作成できません: "
                                    f"{layer.name()} / {provider_index} / {axis.upper()}"
                                )
                            field_name = QgsAuxiliaryLayer.nameFromProperty(definition, True)
                            properties.setProperty(prop, QgsProperty.fromField(field_name))
                        settings.setDataDefinedProperties(properties)
                        labeling.setSettings(settings, provider_id)

            document = QDomDocument()
            document.setContent(xml)
            layout = QgsPrintLayout(project)
            layout.loadFromTemplate(document, QgsReadWriteContext())
            layout.setName(Path(name).name)
            layout.renderContext().setDpi(300)
            for item in layout.items():
                if isinstance(item, QgsLayoutItemMap):
                    item.setKeepLayerSet(True)
                    item.setKeepLayerStyles(False)
                    project.setCrs(item.crs())
                    project.viewSettings().setDefaultViewExtent(
                        QgsReferencedRectangle(item.extent(), item.crs()))
                    project.viewSettings().setDefaultRotation(item.mapRotation())
                    layer_tree = project.layerTreeRoot()
                    layer_tree.reorderGroupLayers(item.layers())
                    if name == "location":
                        # A fixed custom order omits layers added later from the
                        # map canvas render order. The layout itself stays locked.
                        layer_tree.setHasCustomLayerOrder(False)
                    else:
                        layer_tree.setHasCustomLayerOrder(True)
                        layer_tree.setCustomLayerOrder(item.layers())
                elif isinstance(item, QgsLayoutItemPicture) and item.picturePath():
                    source = Path(item.picturePath())
                    if source.is_file():
                        target = project_path.parent / source.name
                        shutil.copy2(source, target)
                        item.setPicturePath(str(target))
            project.layoutManager().addLayout(layout)
            if not project.write():
                raise RuntimeError(f"QGZ保存失敗: {name}: {project.error()}")
            references = [gpkg]
            references.extend(path for path in self.shared_files if any(
                Path(layer.source().split('|')[0]).resolve() == path.resolve()
                for layer in layers.values() if layer.providerType() != 'memory'))
            if name == "location":
                references.append(self.location_directory / "houi2.svg")
            project.clear()
            self._make_project_references_relative(project_path, references)
        instructions = Path(__file__).parent / "qgs_editing.md"
        text = instructions.read_text(encoding="utf-8")
        text += "\n## 今回出力した図面\n\n| QGZ | レイアウト | 書き出し先（出力フォルダ基準） |\n|---|---|---|\n"
        for name, output, _, _ in self.drawings:
            qgz_path = ((self.location_directory / "location.qgz").relative_to(self.directory).as_posix()
                        if name == "location" else f"{name}.qgz")
            text += f"| `{qgz_path}` | `{Path(name).name}` | `{output.replace('|', '&#124;')}` |\n"
        (self.shared_directory / "QGISで図面を編集する方法.md").write_text(text, encoding="utf-8")
        self.drawings.clear()
        self.persistent_layer_ids.clear()

    @staticmethod
    def _make_project_references_relative(project_path, source_paths):
        """Keep selected colocated files relative in an absolute-path QGZ."""
        project_path = Path(project_path)
        replacement_groups = []
        for source_path in source_paths:
            source_path = Path(source_path)
            if not source_path.exists():
                continue
            resolved = source_path.resolve()
            relative_path = Path(os.path.relpath(source_path, project_path.parent)).as_posix()
            if not relative_path.startswith("."):
                relative_path = "./" + relative_path
            replacement_groups.append((
                source_path.name,
                {str(resolved), resolved.as_posix()},
                relative_path,
            ))

        with zipfile.ZipFile(project_path, "r") as source:
            entries = [(info, source.read(info.filename)) for info in source.infolist()]

        replaced_names = set()
        rewritten = []
        for info, data in entries:
            if info.filename.lower().endswith(".qgs"):
                text = data.decode("utf-8")
                # QGISの読込側にも相対パスを解決させる。外部の絶対パスは保持。
                text = re.sub(r'(<properties name="Absolute" type="bool">)true(</properties>)',
                              r'\g<1>false\g<2>', text)
                text = re.sub(r'(<Absolute type="bool">)true(</Absolute>)',
                              r'\g<1>false\g<2>', text)
                for source_name, absolute_paths, relative_path in replacement_groups:
                    for absolute_path in absolute_paths:
                        if absolute_path in text:
                            text = text.replace(absolute_path, relative_path)
                            replaced_names.add(source_name)
                data = text.encode("utf-8")
            rewritten.append((info, data))

        expected_names = {group[0] for group in replacement_groups}
        missing_names = sorted(expected_names - replaced_names)
        if missing_names:
            raise RuntimeError(
                "location.qgz内の同梱ファイル参照を相対化できません: "
                + ", ".join(missing_names)
            )

        handle, temporary_name = tempfile.mkstemp(
            prefix=f".{project_path.stem}_",
            suffix=".qgz",
            dir=project_path.parent,
        )
        os.close(handle)
        try:
            with zipfile.ZipFile(temporary_name, "w") as target:
                for info, data in rewritten:
                    target.writestr(info, data)
            os.replace(temporary_name, project_path)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
