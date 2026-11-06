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
from qgis.PyQt.QtWidgets import QMessageBox
from qgis.core import (
    Qgis, QgsAuxiliaryLayer, QgsFeature, QgsField, QgsGeometry,
    QgsLayoutItemMap, QgsLayoutItemPicture, QgsLayerTreeModel,
    QgsMapLayerStyle, QgsMapThemeCollection, QgsPalLayerSettings,
    QgsPrintLayout, QgsProject, QgsProperty, QgsPropertyDefinition,
    QgsReadWriteContext, QgsReferencedRectangle, QgsVectorFileWriter,
    QgsVectorLayer, QgsGeometryGeneratorSymbolLayer, QgsVariantUtils,
)

from .editable_projects import make_project_references_relative
CONFIG_VERSION = 3

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
            or config.get("mode") != "measurement"):
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
    selected = set(layer.selectedFeatureIds())
    result.selectByIds([f.id() for f in result.getFeatures() if f["fid"] in selected])
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
        for group, pages in (("polygons", self.owner.shuis), ("lines", self.owner.haisuis)):
            for page in pages:
                layer = page.point_layer.currentLayer()
                if layer is None:
                    continue
                key = "input:" + source_key(layer)
                canonical = self.add_dataset(key, full_input(layer))
                registration_uid = getattr(page, 'registration_uid', page.drawing_uid)
                self.inputs[group + ":" + registration_uid] = canonical

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
        auxiliary = layer.auxiliaryLayer()
        if auxiliary is None:
            names = layer.fields().names()
            key = UID if UID in names else "fid" if "fid" in names else None
            # External background layers may have no stored feature ID column.
            # Keep their labels without adding our position storage.
            if key is None:
                return
            field = layer.fields().field(key)
            auxiliary = self.project.auxiliaryStorage().createAuxiliaryLayer(field, layer)
            if auxiliary is not None:
                layer.setAuxiliaryLayer(auxiliary)
        if auxiliary is None:
            raise OSError("ラベル位置の保存領域を作成できません")
        labeling = layer.labeling()
        for provider_index, provider_id in enumerate(labeling.subProviders() or [""], 1):
            settings = labeling.settings(provider_id)
            properties = settings.dataDefinedProperties()
            for property_key, axis in ((QgsPalLayerSettings.PositionX, "x"), (QgsPalLayerSettings.PositionY, "y")):
                definition = QgsPropertyDefinition(f"label_{provider_index}_position_{axis}",
                    f"Label {provider_index} position {axis}", QgsPropertyDefinition.StandardPropertyTemplate.Double, "labeling")
                auxiliary.addAuxiliaryField(definition)
                properties.setProperty(property_key, QgsProperty.fromField(QgsAuxiliaryLayer.nameFromProperty(definition, True)))
            settings.setDataDefinedProperties(properties)
            labeling.setSettings(settings, provider_id)

    def label_visibility_warnings(self):
        warnings = []
        for capture in self.captures:
            for _, dataset_key, _ in capture["layers"]:
                if not dataset_key.startswith("output:"):
                    continue
                old = self.old_layers.get(dataset_key)
                generated = self.datasets[self.aliases[dataset_key]]
                if (old is None or not isinstance(generated, QgsVectorLayer)
                        or "label" not in generated.fields().names()
                        or "label" not in old.fields().names() or not generated.labelsEnabled()):
                    continue
                style_name = "図面:" + capture["key"]
                manager = old.styleManager()
                if style_name not in manager.styles():
                    continue
                probe = old.clone()
                if manager.currentStyle() != style_name:
                    manager.style(style_name).writeToLayer(probe)
                if probe.labelsEnabled():
                    continue
                def text(value):
                    return "" if QgsVariantUtils.isNull(value) else str(value).strip()
                previous = {text(feature[UID]): text(feature["label"])
                            for feature in old.getFeatures()} if UID in old.fields().names() else {}
                previous_names = {text(feature["label"]) for feature in old.getFeatures()}
                changed = any(text(feature["label"]) and
                              (text(feature["label"]) != previous[text(feature[UID])]
                               if text(feature[UID]) in previous else
                               text(feature["label"]) not in previous_names)
                              for feature in generated.getFeatures())
                if changed and capture["title"] not in warnings:
                    warnings.append(capture["title"])
        return warnings

    def save(self):
        self.prepare_inputs()
        unnamed_object_layers = {
            f'output:{prefix}:{page.drawing_uid}:line_layer'
            for prefix, pages in (('shui', getattr(self.owner, 'shuis', [])),
                                  ('haisui', getattr(self.owner, 'haisuis', [])))
            for page in pages if not page.name_edit.text().strip()
        }
        warnings = self.label_visibility_warnings()
        if warnings:
            message = ("名称を変更しましたが、以前のラベル非表示設定が引き継がれます。\n"
                       "次の図面では名称が表示されない可能性があります:\n"
                       + "\n".join(warnings)
                       + "\n\n表示する場合は、QGZで該当図面の地図テーマを適用し、"
                       "レイヤのラベル表示を有効にしてください。")
            self.owner.append_output_log(message)
            if hasattr(self.owner, "warn"):
                self.owner.warn(message)
            else:
                QMessageBox.warning(self.owner, "名称変更時のラベル確認", message)
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
            if old_layout is not None:
                old_map = (self.old_metadata or {}).get("config", {}).get("map", {})
                new_map = self.owner.configuration_data()["map"]
                location = key == "location"
                paper_key = "location_paper" if location else "paper"
                scale_key = "location_scale" if location else "scale"
                old_paper, new_paper = old_map.get(paper_key, "A4"), new_map[paper_key]
                if old_paper != new_paper:
                    self.owner.resize_location_layout(layout, 2 ** (0.5 if new_paper == "A3" else -0.5))
                if old_map.get(scale_key) != new_map.get(scale_key):
                    for item in layout.items():
                        if isinstance(item, QgsLayoutItemMap) and item.customProperty("rz/generated_map", False):
                            item.setScale(new_map[scale_key])
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
                if dataset_key in unnamed_object_layers:
                    # An explicitly blank name overrides a previously saved
                    # object label, while retaining its symbology and all point labels.
                    layer.setLabelsEnabled(False)
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
                    if capture["drawing"] is not None and capture["key"] != "location":
                        from qgis.core import QgsLayoutSize, QgsLayoutPoint, QgsRectangle
                        scale, center = item.scale(), item.extent().center()
                        size = 150 * self.owner.paper_factor()
                        layout.pageCollection().page(0).setPageSize(QgsLayoutSize(size, size))
                        item.attemptResize(QgsLayoutSize(size, size))
                        item.attemptMove(QgsLayoutPoint(0, 0))
                        item.setScale(scale)
                        extent = item.extent()
                        item.setExtent(QgsRectangle(center.x() - extent.width()/2, center.y() - extent.height()/2,
                                                    center.x() + extent.width()/2, center.y() + extent.height()/2))
                    item.setCustomProperty("rz/generated_map", True)
                    # Theme changes in the canvas cannot alter this map's extent/scale/rotation.
                    item.setLayers(visible)
                    item.setKeepLayerSet(False)
                    item.setKeepLayerStyles(False)
                    item.setFollowVisibilityPresetName(theme_name)
                    item.setFollowVisibilityPreset(True)
                    if item.customProperty('rz/generated_map', False) and capture['key'] != 'location':
                        from .map_frame import validate_map_frame
                        measurement_layers = [canonical_layers[self.aliases[dataset_key]]
                                              for _, dataset_key, _ in capture['layers']
                                              if dataset_key.startswith('output:')
                                              and dataset_key.endswith((':pt_layer', ':line_layer'))]
                        if measurement_layers:
                            validate_map_frame(item, measurement_layers, capture['title'])
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

        if descriptors:
            project.mapThemeCollection().applyTheme(descriptors[0]["theme"], root, model)
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
            group, page_id = role.split(":", 1)
            layer = canonical_layers.get(key)
            for item in config[group]["items"]:
                if item["drawing_uid"] == page_id:
                    item["layer"] = self.owner.layer_reference(layer) if layer else None
        config["selected_inputs"] = {}
        for role, canonical in self.inputs.items():
            generated = self.datasets[canonical]
            identity = str(generated.customProperty("rz/source_key", ""))
            selected = set(generated.selectedFeatureIds())
            config["selected_inputs"][identity] = [str(f[UID]) for f in generated.getFeatures() if f.id() in selected]
        config["output"]["directory"] = "../.." if getattr(self.owner, 'output_subdirectory', '') else ".."
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
