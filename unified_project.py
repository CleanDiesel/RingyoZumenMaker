"""One editable project, named styles/themes, durable inputs and regeneration metadata."""
import copy
import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from qgis.PyQt.QtCore import QDate, QVariant
from qgis.PyQt.QtXml import QDomDocument
from qgis.core import (
    Qgis, QgsAuxiliaryLayer, QgsFeature, QgsField, QgsGeometry,
    QgsLayoutItemMap, QgsLayoutItemPicture, QgsLayerTreeModel,
    QgsMapLayerStyle, QgsMapThemeCollection, QgsPalLayerSettings,
    QgsPrintLayout, QgsProject, QgsProperty, QgsPropertyDefinition,
    QgsReadWriteContext, QgsReferencedRectangle, QgsVectorFileWriter,
    QgsVectorLayer, QgsGeometryGeneratorSymbolLayer, QgsVariantUtils,
)

from .editable_projects import make_project_references_relative
from .uav_inputs import CONFIG_VERSION

SCOPE = "RingyoZumenMaker"
UID = "_rz_uid"


def checkpoint_package(path):
    """Fold SQLite's transient WAL into the durable file before a file swap."""
    path = Path(path)
    if not path.is_file():
        return
    with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=5)) as connection:
        busy, _, _ = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if busy:
            raise OSError("GPKGが使用中のため更新できません。他のQGISで閉じてください: " + str(path))


def metadata(project):
    value, ok = project.readEntry(SCOPE, "regeneration", "")
    if not ok or not value:
        return None
    try:
        data = json.loads(value)
    except (ValueError, TypeError):
        return None
    if (not isinstance(data, dict) or data.get("format") != "RingyoZumenMaker.project"
            or data.get("version") != 1):
        return None
    config = data.get("config", {})
    if (not isinstance(config, dict) or config.get("format") != "RingyoZumenMaker.config" or config.get("version") != CONFIG_VERSION
            or config.get("mode") != "uav"):
        return None
    return data


def source_key(layer):
    saved = layer.customProperty("rz/source_key", "")
    if saved:
        return str(saved)
    info = metadata(QgsProject.instance()) or {}
    for key, layer_id in info.get("datasets", {}).items():
        if key.startswith("input:") and layer_id == layer.id():
            return key.split(":", 1)[1]
    identity = layer.id() if layer.providerType() == "memory" else layer.providerType() + ":" + layer.source()
    return str(uuid.uuid5(uuid.NAMESPACE_URL, identity))


def feature_uid(layer, feature):
    if UID in layer.fields().names() and not QgsVariantUtils.isNull(feature[UID]) and str(feature[UID]):
        return str(feature[UID])
    return str(uuid.uuid5(uuid.UUID(source_key(layer)), str(feature.id())))


def json_value(value):
    if isinstance(value, QDate):
        return {"date": value.toString("yyyy-MM-dd")}
    if isinstance(value, dict):
        return {k: json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    if QgsVariantUtils.isNull(value):
        return None
    return value


def restore_value(value):
    if isinstance(value, dict):
        if set(value) == {"date"}:
            return QDate.fromString(value["date"], "yyyy-MM-dd")
        return {k: restore_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [restore_value(v) for v in value]
    return value


def full_input(layer):
    """Copy every feature in the original CRS; preserve original FIDs and attributes."""
    from qgis.core import QgsWkbTypes
    geometry = QgsWkbTypes.displayString(layer.wkbType())
    result = QgsVectorLayer(f"{geometry}?crs={layer.crs().authid()}", layer.name(), "memory")
    result.setCrs(layer.crs())
    fields = list(layer.dataProvider().fields())
    if UID not in [field.name() for field in fields]:
        fields.append(QgsField(UID, QVariant.String, len=36))
    if "fid" not in [field.name() for field in fields]:
        fields.append(QgsField("fid", QVariant.LongLong))
    result.dataProvider().addAttributes(fields)
    result.updateFields()
    for original in layer.getFeatures():
        feature = QgsFeature(result.fields())
        feature.setGeometry(QgsGeometry(original.geometry()))
        values = dict(zip(layer.fields().names(), original.attributes()))
        values[UID] = feature_uid(layer, original)
        values.setdefault("fid", original.id())
        feature.setAttributes([values.get(field.name()) for field in result.fields()])
        if not result.dataProvider().addFeature(feature):
            raise ValueError(f"入力レイヤを保存できません: {layer.name()}")
    result.setCustomProperty("rz/source_key", source_key(layer))
    for i, field in enumerate(result.fields()):
        original_index = layer.fields().indexFromName(field.name())
        if original_index >= 0:
            result.setFieldAlias(i, layer.attributeAlias(original_index))
    result.setRenderer(layer.renderer().clone())
    if layer.labeling():
        result.setLabeling(layer.labeling().clone())
        result.setLabelsEnabled(layer.labelsEnabled())
    result.updateExtents()
    return result


def signature(layer):
    digest = hashlib.sha256()
    digest.update(layer.crs().toWkt().encode())
    indexes = [i for i, field in enumerate(layer.fields()) if field.name() != UID]
    digest.update(repr([(layer.fields()[i].name(), layer.fields()[i].typeName(), layer.fields()[i].length(), layer.fields()[i].precision()) for i in indexes]).encode())
    records = []
    for feature in layer.getFeatures():
        records.append(bytes(feature.geometry().asWkb()) + repr([feature[i] for i in indexes]).encode("utf-8"))
    for record in sorted(records):
        digest.update(record)
    return digest.hexdigest()


class UnifiedProjects:
    def __init__(self, owner, directory, snapshot=None, copied_ortho=None):
        self.owner = owner
        self.directory = Path(directory)
        self.qgz_dir = self.directory / "qgz"
        self.gpkg = self.qgz_dir / "ringyo_zumen.gpkg"
        self.project_path = self.qgz_dir / owner.project_file_name()
        self.snapshot = snapshot
        self.copied_ortho = copied_ortho
        self.captures = []
        self.inputs = {}
        self.datasets = {}
        self.aliases = {}
        self.signatures = {}
        # GDAL caches connections by path and open options. A new generation must
        # not reuse a connection to the retired file at the same path.
        self.connection_option = "|option:PRELUDE_STATEMENTS=SELECT 1 /*rz_" + uuid.uuid4().hex + "*/"
        self.project = QgsProject()
        self.old_metadata = None
        if snapshot:
            if not self.project.read(str(snapshot)):
                raise OSError("編集済みプロジェクトの一時保存を読み込めません")
            self.old_metadata = metadata(self.project)
        self.old_layers = {}
        for key, layer_id in (self.old_metadata or {}).get("datasets", {}).items():
            layer = self.project.mapLayer(layer_id)
            if layer is not None:
                self.old_layers[key] = layer
        for layer in self.project.mapLayers().values():
            for key in json.loads(str(layer.customProperty("rz/keys", "[]"))):
                self.old_layers[key] = layer

    def add_dataset(self, key, layer):
        if isinstance(layer, QgsVectorLayer) and layer.featureCount() == 0:
            return None
        content = signature(layer) if isinstance(layer, QgsVectorLayer) else layer.providerType() + ":" + layer.source()
        canonical = self.signatures.get(content)
        if canonical is None:
            canonical = key
            self.signatures[content] = canonical
            self.datasets[key] = layer.clone()
            if key.startswith("input:"):
                self.datasets[key].setCustomProperty("rz/source_key", key.split(":", 1)[1])
        self.aliases[key] = canonical
        return canonical

    def capture(self, layout, key, title, output, layers, drawing=None):
        document = QDomDocument()
        document.appendChild(layout.writeXml(document, QgsReadWriteContext()))
        self.captures.append({"key": key, "title": title, "output": output,
                              "xml": document.toString(), "layers": layers, "drawing": drawing})

    def prepare_inputs(self):
        from .uav_inputs import LAYER_INPUTS
        for role in LAYER_INPUTS:
            layer = getattr(self.owner, role).currentLayer()
            if layer is None:
                continue
            key = "input:" + source_key(layer)
            if isinstance(layer, QgsVectorLayer):
                copied = full_input(layer)
            else:
                copied = layer.clone()
                copied.setCustomProperty("rz/source_key", source_key(layer))
                if self.copied_ortho and Path(layer.source().split("|")[0]).resolve() == Path(self.owner.assignmentOlso.filePath()).resolve():
                    copied.setDataSource(str(self.copied_ortho), layer.name(), layer.providerType())
            canonical = self.add_dataset(key, copied)
            self.inputs[role] = canonical

    def _write_data(self):
        self.qgz_dir.mkdir(parents=True, exist_ok=True)
        old_gpkg = None
        if self.old_metadata:
            old_gpkg = Path(self.snapshot).parent / "unused"
            for layer in self.old_layers.values():
                if isinstance(layer, QgsVectorLayer) and layer.providerType() == "ogr":
                    path = Path(layer.source().split("|")[0])
                    if path.name == "ringyo_zumen.gpkg" and path.is_file():
                        old_gpkg = path
                        break
            if old_gpkg.is_file():
                # Retain user-created tables; only managed tables are replaced.
                with closing(sqlite3.connect(str(old_gpkg))) as source, closing(sqlite3.connect(str(self.gpkg))) as target:
                    source.backup(target)
                with closing(sqlite3.connect(str(self.gpkg))) as connection, connection:
                    for table in self.old_metadata.get("tables", []):
                        quoted = '"' + table.replace('"', '""') + '"'
                        connection.execute("DROP TABLE IF EXISTS " + quoted)
                        for metadata_table in ("gpkg_contents", "gpkg_geometry_columns", "gpkg_extensions", "gpkg_ogr_contents", "gpkg_data_columns", "gpkg_metadata_reference"):
                            if connection.execute("SELECT 1 FROM sqlite_master WHERE name=?", (metadata_table,)).fetchone():
                                connection.execute(f'DELETE FROM "{metadata_table}" WHERE table_name=?', (table,))
                        for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ?", ("rtree_" + table + "_%",)).fetchall():
                            connection.execute('DROP TABLE IF EXISTS "' + name.replace('"', '""') + '"')
        tables = []
        sources = {}
        for key, layer in self.datasets.items():
            if not isinstance(layer, QgsVectorLayer) or key.startswith("background:"):
                continue
            table = "入力_" if key.startswith("input:") else "出力_"
            table += hashlib.sha256(key.encode()).hexdigest()[:16]
            options = QgsVectorFileWriter.SaveVectorOptions()
            options.driverName = "GPKG"
            options.layerName = table
            options.fileEncoding = "UTF-8"
            options.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteLayer if self.gpkg.exists() else QgsVectorFileWriter.CreateOrOverwriteFile
            result = QgsVectorFileWriter.writeAsVectorFormatV3(layer, str(self.gpkg), QgsProject.instance().transformContext(), options)
            if result[0] != QgsVectorFileWriter.NoError:
                raise OSError(f"GPKG保存失敗: {layer.name()}: {result[1]}")
            tables.append(table)
            sources[key] = f"{self.gpkg}|layername={table}" + self.connection_option
        return tables, sources

    def _auxiliary(self, layer):
        if not isinstance(layer, QgsVectorLayer) or not layer.labelsEnabled():
            return
        field = layer.fields().field(UID if UID in layer.fields().names() else "fid")
        auxiliary = layer.auxiliaryLayer()
        if auxiliary is None:
            auxiliary = self.project.auxiliaryStorage().createAuxiliaryLayer(field, layer)
            if auxiliary is not None:
                layer.setAuxiliaryLayer(auxiliary)
        if auxiliary is None:
            raise OSError("ラベル位置の保存領域を作成できません")
        settings = layer.labeling().settings()
        properties = settings.dataDefinedProperties()
        for property_key, axis in ((QgsPalLayerSettings.PositionX, "x"), (QgsPalLayerSettings.PositionY, "y")):
            definition = QgsPropertyDefinition("label_position_" + axis, "Label position " + axis, QgsPropertyDefinition.StandardPropertyTemplate.Double, "labeling")
            auxiliary.addAuxiliaryField(definition)
            properties.setProperty(property_key, QgsProperty.fromField(QgsAuxiliaryLayer.nameFromProperty(definition, True)))
        settings.setDataDefinedProperties(properties)
        layer.labeling().setSettings(settings)

    def save(self):
        self.prepare_inputs()
        tables, sources = self._write_data()
        project = self.project
        project.setFileName(str(self.project_path))
        project.setFilePathStorage(Qgis.FilePathType.Absolute)
        project.setTransformContext(QgsProject.instance().transformContext())
        project.setCrs(self.owner.crs.crs())
        canonical_layers = {}
        for key, generated in self.datasets.items():
            old = next((self.old_layers[alias] for alias, canonical in self.aliases.items() if canonical == key and alias in self.old_layers), None)
            layer = old if old is not None else generated.clone()
            style = QgsMapLayerStyle()
            style.readFromLayer(layer)
            style_manager = layer.styleManager()
            current_name = style_manager.currentStyle()
            named_styles = {name: QgsMapLayerStyle(style_manager.style(name).xmlData())
                            for name in style_manager.styles()}
            named_styles[current_name] = style
            if key in sources:
                layer.setDataSource(sources[key], generated.name(), "ogr")
            elif old is not None and old.source() != generated.source():
                layer.setDataSource(generated.source(), generated.name(), generated.providerType())
            if not layer.isValid():
                raise OSError(f"保存レイヤを開けません: {generated.name()}")
            style.writeToLayer(layer)
            if old is not None:
                style_manager.reset()
                for name, saved_style in named_styles.items():
                    if name:
                        style_manager.addStyle(name, saved_style)
                style_manager.setCurrentStyle(current_name)
                style.writeToLayer(layer)
            layer.setCustomProperty("rz/keys", json.dumps([alias for alias, canonical in self.aliases.items() if canonical == key]))
            if generated.customProperty("rz/source_key", ""):
                layer.setCustomProperty("rz/source_key", generated.customProperty("rz/source_key"))
            if old is None:
                project.addMapLayer(layer)
            self._auxiliary(layer)
            canonical_layers[key] = layer

        obsolete = [layer.id() for layer in project.mapLayers().values()
                    if layer in self.old_layers.values() and layer not in canonical_layers.values()]
        project.removeMapLayers(obsolete)
        # User-added GPKG layers must refer to the new file, not its retired version.
        for layer in project.mapLayers().values():
            if isinstance(layer, QgsVectorLayer) and layer not in canonical_layers.values() and "ringyo_zumen.gpkg|" in layer.source():
                suffix = layer.source().split("|", 1)[1]
                layer.setDataSource(str(self.gpkg) + "|" + suffix, layer.name(), "ogr")

        descriptors = []
        generated_layout_keys = set()
        generated_theme_names = set()
        root = project.layerTreeRoot()
        model = QgsLayerTreeModel(root)
        for capture in self.captures:
            key = capture["key"]
            generated_layout_keys.add(key)
            theme_name = capture["title"]
            old_layout = next((layout for layout in project.layoutManager().layouts() if layout.customProperty("rz/key", "") == key), None)
            if old_layout is None:
                document = QDomDocument()
                xml = capture["xml"]
                for original_id, dataset_key, _ in capture["layers"]:
                    xml = xml.replace(original_id, canonical_layers[self.aliases[dataset_key]].id())
                document.setContent(xml)
                layout = QgsPrintLayout(project)
                layout.loadFromTemplate(document, QgsReadWriteContext())
                layout.setName(theme_name)
                layout.setCustomProperty("rz/key", key)
                project.layoutManager().addLayout(layout)
            else:
                layout = old_layout
                previous = next((item for item in self.old_metadata.get("layouts", []) if item["key"] == key), {})
                theme_name = previous.get("theme", layout.name())
                old_title = (previous.get("drawing") or {}).get("folder", "位置図")
                if layout.name() == old_title and capture["title"] != old_title:
                    title = capture["title"]
                    suffix = 2
                    while project.layoutManager().layoutByName(title) is not None:
                        title = f"{capture['title']} ({suffix})"
                        suffix += 1
                    layout.setName(title)
                    if theme_name == old_title and not project.mapThemeCollection().hasMapTheme(title):
                        project.mapThemeCollection().renameMapTheme(theme_name, title)
                        theme_name = title
            generated_theme_names.add(theme_name)
            visible = []
            for _, dataset_key, captured_style in capture["layers"]:
                layer = canonical_layers[self.aliases[dataset_key]]
                style_name = "図面:" + key
                manager = layer.styleManager()
                if style_name not in manager.styles():
                    # HTML label positioning is shared by both named styles, never duplicated data.
                    current = QgsMapLayerStyle()
                    current.readFromLayer(layer)
                    captured_style.writeToLayer(layer)
                    if key == "aggregate" and dataset_key == "output:aggregate" and self.inputs.get("polygon"):
                        # Show the actual removed part without persisting an intermediate dataset.
                        source = canonical_layers[self.inputs["polygon"]]
                        expression = ("difference(transform(geometry(get_feature('" + source.id() + "','" + UID + "',\"" + UID + "\")), '" +
                                      source.crs().authid() + "', '" + layer.crs().authid() + "'), $geometry)")
                        generator = QgsGeometryGeneratorSymbolLayer.create({"geometryModifier": expression, "SymbolType": "Fill"})
                        from qgis.core import QgsFillSymbol
                        generator.setSubSymbol(QgsFillSymbol.createSimple({"color": "0,80,255,90", "outline_color": "0,80,255,255", "outline_width": "0.3"}))
                        layer.renderer().symbol().appendSymbolLayer(generator)
                    self._auxiliary(layer)
                    adjusted = QgsMapLayerStyle()
                    adjusted.readFromLayer(layer)
                    manager.addStyle(style_name, adjusted)
                    current.writeToLayer(layer)
                manager.setCurrentStyle(style_name)
                visible.append(layer)
            if not project.mapThemeCollection().hasMapTheme(theme_name):
                for node in root.findLayers():
                    node.setItemVisibilityChecked(node.layer() in visible)
                root.setHasCustomLayerOrder(True)
                remaining = [layer for layer in project.mapLayers().values() if layer not in visible]
                order = visible + remaining
                if not self.old_metadata:
                    references = canonical_layers.get(self.aliases.get("output:references"))
                    if references in order:
                        order.remove(references)
                        order.insert(0, references)
                root.setCustomLayerOrder(order)
                record = QgsMapThemeCollection.createThemeFromCurrentState(root, model)
                project.mapThemeCollection().insert(theme_name, record)
            else:
                record = project.mapThemeCollection().mapThemeState(theme_name)
                existing = [item.layer() for item in record.layerRecords()]
                for layer in visible:
                    if layer not in existing and layer not in self.old_layers.values():
                        addition = QgsMapThemeCollection.MapThemeLayerRecord(layer)
                        addition.isVisible = True
                        addition.usingCurrentStyle = True
                        addition.currentStyle = layer.styleManager().currentStyle()
                        record.addLayerRecord(addition)
                project.mapThemeCollection().update(theme_name, record)
            for item in layout.items():
                if isinstance(item, QgsLayoutItemMap):
                    if old_layout is not None and not item.customProperty("rz/generated_map", False):
                        continue
                    item.setCustomProperty("rz/generated_map", True)
                    # Theme changes in the canvas cannot alter this map's extent/scale/rotation.
                    item.setLayers(visible)
                    item.setKeepLayerSet(False)
                    item.setKeepLayerStyles(False)
                    item.setFollowVisibilityPresetName(theme_name)
                    item.setFollowVisibilityPreset(True)
                    if not descriptors:
                        project.viewSettings().setDefaultViewExtent(QgsReferencedRectangle(item.extent(), item.crs()))
                elif isinstance(item, QgsLayoutItemPicture) and item.picturePath():
                    source = Path(item.picturePath())
                    if source.is_file():
                        target = self.qgz_dir / source.name
                        if source.resolve() != target.resolve():
                            shutil.copy2(source, target)
                        item.setPicturePath(str(target))
            descriptors.append({"key": key, "layout": layout.name(), "theme": theme_name,
                                "output": capture["output"], "drawing": capture["drawing"]})
        old_generated_themes = {item["theme"] for item in (self.old_metadata or {}).get("layouts", [])}
        for layout in list(project.layoutManager().layouts()):
            if layout.customProperty("rz/key", "") and layout.customProperty("rz/key") not in generated_layout_keys:
                project.layoutManager().removeLayout(layout)
        for name in old_generated_themes - generated_theme_names:
            project.mapThemeCollection().removeMapTheme(name)

        config = copy.deepcopy(self.owner.configuration_data())
        # Applying a QGIS style can replace custom properties as well as symbology.
        # Reattach identities after the last style switch.
        for key, layer in canonical_layers.items():
            identities = [alias for alias, canonical in self.aliases.items() if canonical == key]
            layer.setCustomProperty("rz/keys", json.dumps(identities))
            input_identity = next((alias.split(":", 1)[1] for alias in identities if alias.startswith("input:")), None)
            if input_identity:
                layer.setCustomProperty("rz/source_key", input_identity)
        for role, key in self.inputs.items():
            layer = canonical_layers.get(key)
            config["layers"][role] = self.owner.layer_reference(layer) if layer else None
        config["output"]["directory"] = ".."
        if self.copied_ortho:
            config["output"]["assignment_ortho"] = os.path.relpath(self.copied_ortho, self.qgz_dir).replace("\\", "/")
        info = {"format": "RingyoZumenMaker.project", "version": 1, "config": config,
                "layouts": descriptors, "tables": tables,
                "datasets": {alias: canonical_layers[canonical].id() for alias, canonical in self.aliases.items()}}
        project.writeEntry(SCOPE, "regeneration", json.dumps(json_value(info), ensure_ascii=False))
        if not project.write():
            raise OSError("統合QGZを保存できません: " + project.error())
        references = [self.gpkg]
        if self.copied_ortho:
            references.append(self.copied_ortho)
        references.extend(path for path in self.qgz_dir.glob("*.svg"))
        project.clear()
        make_project_references_relative(self.project_path, references)
        if not project.read(str(self.project_path)):
            raise OSError("統合QGZを検証できません")
        for layer in project.mapLayers().values():
            if layer.customProperty("rz/keys", "") and not layer.isValid():
                raise OSError("統合QGZの参照が無効です: " + layer.name())
        self.info = metadata(project)
        return project, self.info
