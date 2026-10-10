"""UAV geometry, application attributes, and drawing output."""
import math
import os
import shutil
import tempfile
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

from lxml import etree as ET
from qgis.PyQt.QtCore import QDate, QVariant
from qgis.PyQt.QtGui import QColor, QFont
from qgis.PyQt.QtWidgets import QMessageBox
from qgis.PyQt.QtXml import QDomDocument
from qgis.core import (
    Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
    QgsExpression, QgsExpressionContext, QgsExpressionContextUtils,
    QgsFeature, QgsField, QgsGeometry, QgsLayoutExporter, QgsLayoutItemMap,
    QgsLayoutPoint, QgsLayoutSize, QgsMapLayerProxyModel, QgsPalLayerSettings,
    QgsPrintLayout, QgsProject, QgsReadWriteContext, QgsVectorFileWriter, QgsMapLayerStyle, QgsFillSymbol,
    QgsLineSymbol, QgsMarkerSymbol, QgsSingleSymbolRenderer,
    QgsVariantUtils, QgsVectorLayer, QgsVectorLayerSimpleLabeling, QgsWkbTypes,
)

from .unified_project import UnifiedProjects, UID, feature_uid, source_key, metadata, checkpoint_package
from .project_session import ProjectSession
from .uav_inputs import UavInputs


ROOT = Path(__file__).parent
ATTRIBUTE_DEFINITIONS = (
    ("振興局", "振興局名", QVariant.String, 40, 0),
    ("市町村", "市町村名", QVariant.String, 80, 0),
    ("林班", "林班", QVariant.Int, 9, 0),
    ("小班", "小班", QVariant.Int, 9, 0),
    ("枝番", "林小班枝番(No)", QVariant.String, 40, 0),
    ("製図日", "製図年月日", QVariant.Date, 0, 0),
    ("製図者", "製図者名", QVariant.String, 100, 0),
    ("所有者", "森林所有者名", QVariant.String, 100, 0),
    ("事業", "事業の種類", QVariant.String, 20, 0),
    ("申請No", "申請番号(親番－枝番)", QVariant.String, 100, 0),
    ("面積ha", "面積(ha)", QVariant.Double, 20, 5),
    ("申請ha", "申請合計面積", QVariant.Double, 20, 2),
    ("更新ha", "更新面積", QVariant.Double, 20, 2),
)
LINE_ATTRIBUTE_DEFINITIONS = ATTRIBUTE_DEFINITIONS[:-3] + (
    ("幅m", "幅(m)", QVariant.Double, 20, 2),
    ("延長m", "延長(m)", QVariant.LongLong, 18, 0),
)


class UavWorkflow(ProjectSession, UavInputs):
    def on_submit(self, test=False):
        self.open_output_tab()
        self.clear_output_log()
        self.progressBar.setValue(0)
        staging = None
        session = None
        closed = False
        original_project = QgsProject.instance().fileName()
        self._retained_output_paths = set()
        try:
            create_project = self.outputMode.currentIndex() == 0
            if not test and create_project and not self.settle_project_edits():
                return
            if create_project and not test:
                session = tempfile.TemporaryDirectory(prefix="ringyo_project_")
                self._project_snapshot = self.snapshot_project(Path(session.name) / "current.qgz")
            self.validate_inputs()
            self.calculate_uav()
            for drawing in self.drawings:
                self.activate_drawing(drawing)
                self.append_output_log(f"【{drawing['folder']}】")
                self.log_calculation()
            self.activate_drawing(self.drawings[0])
            self.progressBar.setValue(40)
            if test:
                self.append_output_log("試算完了。ファイルは生成していません。")
                self.progressBar.setValue(100)
                return
            final = Path(self.fileName.filePath())
            staging = Path(tempfile.mkdtemp(prefix=".ringyo_zumen_tmp_", dir=final))
            self._output_dir_override = staging
            self._final_output_dir = final
            self.export_uav()
            self.progressBar.setValue(80)
            if self.isSaveConfig.currentIndex() == 1 and not self.save_config_file():
                return
            # Release OGR handles before replacing the package currently open in QGIS.
            if self._editable_projects is not None:
                self._editable_projects.project.clear()
                self._editable_projects = None
            target_package = (final / "qgz/ringyo_zumen.gpkg").resolve()
            references_target = any(layer.providerType() == "ogr" and
                                    Path(layer.source().split("|")[0]).resolve() == target_package
                                    for layer in QgsProject.instance().mapLayers().values())
            replacing_open = bool(create_project and getattr(self, "_project_snapshot", None) and
                                  (Path(original_project).resolve().is_relative_to(final.resolve()) or references_target))
            project_filename = self.project_file_name()
            if replacing_open:
                if self.backupQgz.isChecked():
                    saved = QgsProject()
                    original_path = Path(original_project)
                    backup_project = (original_path if original_path.suffix.lower() == ".qgz"
                                      and original_path.resolve().parent == (final / "qgz").resolve()
                                      else final / "qgz" / project_filename)
                    if not saved.read(str(self._project_snapshot)) or not saved.write(str(backup_project)):
                        raise OSError("バックアップ用に現在のプロジェクトを保存できません")
                    saved.clear()
                QgsProject.instance().clear()
                closed = True
                checkpoint_package(target_package)
            def reopen_updated_project():
                if not QgsProject.instance().read(str(final / "qgz" / project_filename)):
                    QgsProject.instance().clear()
                    raise OSError("更新したプロジェクトを開けません")
                for layer in QgsProject.instance().mapLayers().values():
                    if layer.providerType() == "ogr" and Path(layer.source().split("|")[0]).resolve() == target_package:
                        layer.dataProvider().reloadData()
                        if not layer.isValid() or layer.featureCount() < 0:
                            name = layer.name()
                            QgsProject.instance().clear()
                            raise OSError("更新したGPKGを読み込めません: " + name)
            if not self.commit_staged_output(staging, final, self.backupQgz.isChecked(),
                                             preserve_projects=not create_project,
                                             validate_commit=reopen_updated_project if closed else None):
                return
            if closed:
                closed = False
            if self.isSaveConfig.currentIndex() != 1 and not self.save_config_file():
                return
            self.append_output_log(f"出力を確定しました: {final}")
            self.progressBar.setValue(100)
        except Exception as error:
            self.append_output_log(f"エラー: {error}")
            QMessageBox.warning(self, "エラー", str(error))
            self.progressBar.setValue(0)
        finally:
            if closed and self._project_snapshot:
                QgsProject.instance().read(str(self._project_snapshot))
                QgsProject.instance().setFileName(original_project)
                self.restore_project_settings()
            if session:
                session.cleanup()
            # QGZのクローンレイヤを解放してから一時フォルダを片づける。
            if hasattr(self, "_editable_projects"):
                self._editable_projects = None
            for attribute in ("_output_dir_override", "_final_output_dir", "_drawing_output_dir", "_project_snapshot", "_export_map_scale", "_retained_output_paths"):
                if hasattr(self, attribute):
                    delattr(self, attribute)
            if (staging is not None and staging.exists() and not staging.is_symlink()
                    and staging.name.startswith(".ringyo_zumen_tmp_")
                    and staging.resolve().parent == Path(self.fileName.filePath()).resolve()):
                shutil.rmtree(staging, ignore_errors=True)

    def checked_geometry(self, layer, feature):
        geometry = QgsGeometry(feature.geometry())
        errors = geometry.validateGeometry(Qgis.GeometryValidationEngine.Geos)
        if geometry.isNull() or geometry.isEmpty() or errors:
            reason = "; ".join(error.what() for error in errors) or "空の形状です"
            raise ValueError(f"{layer.name()} 地物ID={feature.id()}: 不正な形状（自己交差など）: {reason}")
        if not layer.crs().isValid():
            raise ValueError(f"{layer.name()}: レイヤのCRSがありません")
        if layer.crs() != self.crs.crs():
            geometry.transform(QgsCoordinateTransform(layer.crs(), self.crs.crs(), QgsProject.instance()))
        if not geometry.isGeosValid():
            raise ValueError(f"{layer.name()} 地物ID={feature.id()}: 座標変換後の形状が不正です")
        return geometry

    @staticmethod
    def polygon_parts(geometry):
        if geometry.isEmpty() or geometry.isNull():
            return []
        if geometry.type() == QgsWkbTypes.PolygonGeometry:
            if geometry.isMultipart():
                return [QgsGeometry(part.clone()) for part in geometry.constGet().parts()]
            return [QgsGeometry(geometry)]
        return [part for child in geometry.asGeometryCollection()
                for part in UavWorkflow.polygon_parts(child)]

    @staticmethod
    def checked_operation(geometry, description):
        if geometry.lastError():
            raise ValueError(f"{description}: {geometry.lastError()}")
        if not geometry.isEmpty() and not geometry.isNull() and not geometry.isGeosValid():
            raise ValueError(f"{description}: 演算結果の形状が不正です")
        return geometry

    @staticmethod
    def hectares(value, decimals):
        return (Decimal(str(value)) / Decimal("10000")).quantize(
            Decimal(1).scaleb(-decimals), rounding=ROUND_DOWN
        )

    def memory_polygon_layer(self, name, records, application_fields=False, geometry_type="MultiPolygon"):
        layer = QgsVectorLayer(f"{geometry_type}?crs={self.crs.crs().authid()}", name, "memory")
        fields = [QgsField("label", QVariant.String, len=254), QgsField("source_id", QVariant.LongLong), QgsField(UID, QVariant.String, len=36)]
        if application_fields:
            fields.extend(QgsField(field, kind, len=length, prec=precision)
                          for field, _, kind, length, precision in application_fields)
        else:
            fields.append(QgsField("area_m2", QVariant.Double))
            if any("延長m" in attributes for _, attributes in records):
                fields.append(QgsField("延長m", QVariant.LongLong))
        layer.dataProvider().addAttributes(fields)
        layer.updateFields()
        for geometry, attributes in records:
            geometry = QgsGeometry(geometry)
            if geometry.isNull():
                # 全部除地となった元地物もレコードは保持する。
                geometry = QgsGeometry.fromWkt("MULTIPOLYGON EMPTY")
            geometry.convertToMultiType()
            feature = QgsFeature(layer.fields())
            feature.setGeometry(geometry)
            feature.setAttributes([attributes.get(field.name()) for field in layer.fields()])
            if not layer.dataProvider().addFeature(feature):
                raise ValueError(f"{name}: 地物を作成できません")
        for field, alias, _, _, _ in application_fields or ATTRIBUTE_DEFINITIONS:
            index = layer.fields().indexFromName(field)
            if index >= 0:
                layer.setFieldAlias(index, alias)
        layer.updateExtents()
        return layer

    def calculate_uav(self):
        """Build the aggregate drawing and one drawing per selected source feature."""
        self.calculate_reference_points()
        self.drawings = []
        used_names = set()
        polygons = []
        source = self.polygon.currentLayer()
        if source is not None:
            for feature in self.selected_features("polygon", "polygonFilter"):
                geometry = self.checked_geometry(source, feature)
                name = self.clean_html_text(self.evaluate_attribute("polygonName", source, feature)).strip()
                polygons.append((geometry, self.polygon_attributes(source, feature, name)))
                polygons[-1][1][UID] = feature_uid(source, feature)
        if polygons:
            exclusion_sources = []
            for combo_name, filter_name, expression, is_road in (
                ("jochiPolygon", "jochiFilter", "jochiName", False),
                ("sagyodoLine", "sagyodoFilter", "jochiNameSagyodo", True),
            ):
                layer = getattr(self, combo_name).currentLayer()
                if layer is None:
                    continue
                for feature in self.selected_features(combo_name, filter_name):
                    geometry = self.checked_geometry(layer, feature)
                    width = self.numeric_override("hukuin", layer, feature) if is_road else None
                    if is_road:
                        geometry = self.buffer_line(geometry, width)
                    name = self.clean_html_text(self.evaluate_attribute(expression, layer, feature)).strip()
                    description = "・".join(filter(None, (name, f"幅{width:g}m"))) if is_road else name
                    line_data = (self.checked_geometry(layer, feature), width) if is_road else None
                    exclusion_sources.append((geometry, name, description, line_data))
            self.build_application_geometry(polygons, exclusion_sources)
            self.remember_drawing("申請区域", "全体", None, used_names,
                                  [attributes["製図事業者"] for _, attributes in polygons])

        for combo_name, is_line in (("singleLine", True),):
            source = getattr(self, combo_name).currentLayer()
            if source is None:
                continue
            for index, feature in enumerate(self.selected_features(combo_name, combo_name + "Filter"), 1):
                geometry = self.checked_geometry(source, feature)
                name = self.clean_html_text(self.evaluate_attribute(combo_name + "Name", source, feature)).strip()
                attributes = self.polygon_attributes(source, feature, name, combo_name)
                attributes[UID] = feature_uid(source, feature)
                if is_line:
                    width = self.numeric_override("singleLineWidth", source, feature)
                    attributes.update({"幅m": width, "延長m": math.floor(geometry.length())})
                self.build_ancillary_line(geometry, attributes)
                self.remember_drawing("付帯作工物",
                                      name or f"地物{index}", attributes, used_names)
        if not self.drawings:
            raise ValueError("施行地または付帯作工物で、少なくとも1つの対象地物を指定してください")
        self.activate_drawing(self.drawings[0])

    def build_ancillary_line(self, geometry, attributes):
        self.work_area = self.exclusion_area = self.application_area = 0
        self.work_terms = []
        self.exclusion_terms = []
        self.excluded_small_parts = []
        self.application_layer = self.memory_polygon_layer(
            "付帯作工物", [(geometry, attributes)], LINE_ATTRIBUTE_DEFINITIONS, "MultiLineString")
        self.original_layer = self.application_layer
        self.exclusion_layer = self.memory_polygon_layer("除地（なし）", [])
        self.result_layers = [self.application_layer]
        if self.reference_layer is not None:
            self.result_layers.append(self.reference_layer)
        self.apply_polygon_style(self.application_layer, "polygon.qml")

    def buffer_line(self, geometry, width):
        result = self.checked_operation(geometry.buffer(
            width / 2, 1, Qgis.EndCapStyle.Flat, Qgis.JoinStyle.Miter, 10), "ラインバッファ")
        if result.isEmpty() or result.isNull() or result.area() <= 0:
            raise ValueError("ラインから幅付きポリゴンを作成できません")
        return result

    @staticmethod
    def line_calculation_label(name, length, width):
        length_text = str(math.floor(length))
        return "・".join(filter(None, (name, f"延長{length_text}m × 幅{width:g}m")))

    def remember_drawing(self, kind, title, attributes, used_names, header_companies=None):
        name = self.safe_file_name(f"{kind} - {title}")
        base = name
        suffix = 2
        while name.casefold() in used_names:
            name = f"{base} ({suffix})"
            suffix += 1
        used_names.add(name.casefold())
        for layer in self.result_layers:
            if layer is not self.reference_layer:
                layer.setName(f"{name} - {layer.name()}")
        state = {key: getattr(self, key) for key in (
            "work_area", "exclusion_area", "application_area", "work_terms", "exclusion_terms",
            "excluded_small_parts", "original_layer", "exclusion_layer", "application_layer",
            "result_layers", "reference_layer", "reference_distance")}
        key = "aggregate" if attributes is None else "line:" + attributes[UID]
        folder = (Path("付帯作工物") / name).as_posix() if attributes is not None else name
        state.update({"folder": folder, "title": title, "kind": kind, "attributes": attributes, "key": key})
        if header_companies is not None:
            state["header_companies"] = header_companies
        self.drawings.append(state)

    def activate_drawing(self, drawing):
        self._current_drawing = drawing
        for key, value in drawing.items():
            if key not in ("folder", "title", "kind", "attributes"):
                setattr(self, key, value)

    @staticmethod
    def compartment_name(attributes, include_branch=False):
        parts = []
        for field in ("林班", "小班"):
            value = attributes[field]
            if not QgsVariantUtils.isNull(value) and str(value).strip():
                parts.append(f"{value}{field}")
        compartment = "".join(parts)
        if include_branch:
            branch = attributes["枝番"]
            if not QgsVariantUtils.isNull(branch) and str(branch).strip():
                compartment += f"（枝番{branch}）"
        return compartment

    def header_values(self, values):
        texts = []
        for value in values:
            if QgsVariantUtils.isNull(value):
                continue
            text = (value.toString("yyyy年MM月dd日") if isinstance(value, QDate)
                    else self.clean_html_text(value).strip())
            if text and text not in texts:
                texts.append(text)
        return "、".join(texts)

    def drawing_metadata(self):
        drawing = self._current_drawing
        attributes = drawing["attributes"]
        if attributes is None:
            features = list(drawing["application_layer"].getFeatures())
            compartments = []
            for feature in features:
                compartment = self.compartment_name(feature)
                if compartment and compartment not in compartments:
                    compartments.append(compartment)
            return {"name": "、".join(compartments),
                    "date": self.header_values(feature["製図日"] for feature in features),
                    "draftsperson": self.header_values(feature["製図者"] for feature in features),
                    "owner": self.header_values(feature["所有者"] for feature in features),
                    "company": self.header_values(drawing.get("header_companies", [])),
                    "application_no": self.header_values(feature["申請No"] for feature in features)}
        compartment = self.compartment_name(attributes, include_branch=True)
        name = " ".join(filter(None, (compartment, drawing["title"])))
        return {"name": name, "date": self.header_values([attributes["製図日"]]), "draftsperson": attributes["製図者"],
                "owner": attributes["所有者"], "application_no": attributes["申請No"],
                "company": attributes.get("製図事業者", "")}

    def build_application_geometry(self, polygons, exclusion_sources, single=False):
        exclusions_union = self.checked_operation(
            QgsGeometry.unaryUnion([geometry for geometry, _, _, _ in exclusion_sources]),
            "除地の重複統合",
        ) if exclusion_sources else QgsGeometry()
        accepted = []
        results = []
        self.excluded_small_parts = []
        minimum = self.minJochi.value() * 100
        for geometry, attributes in polygons:
            clipped = self.checked_operation(geometry.intersection(exclusions_union), "除地との重なり") if exclusion_sources else QgsGeometry()
            accepted_parts = []
            for part in self.polygon_parts(clipped):
                names = []
                descriptions = []
                line_lengths = []
                for exclusion_geometry, name, description, line_data in exclusion_sources:
                    if part.intersection(exclusion_geometry).area() > 1e-8:
                        if name and name not in names:
                            names.append(name)
                        if line_data is not None:
                            centerline, width = line_data
                            length = self.checked_operation(centerline.intersection(part), "除地ライン延長").length()
                            line_lengths.append(math.floor(length))
                            description = self.line_calculation_label(name, length, width)
                        if description and description not in descriptions:
                            descriptions.append(description)
                label = "・".join(names)
                # 閾値は表示丸め前の面積へ適用する。
                if part.area() < minimum:
                    self.excluded_small_parts.append((label, part.area()))
                    continue
                accepted_parts.append(part)
                accepted.append((part, {"label": label, "source_id": attributes["source_id"],
                                        "延長m": sum(line_lengths) if line_lengths else None,
                                        "calculation_label": "・".join(descriptions),
                                        "area_m2": part.area()}))
            removed = QgsGeometry.unaryUnion(accepted_parts) if accepted_parts else QgsGeometry()
            result = self.checked_operation(geometry.difference(removed), "除地の除去") if accepted_parts else QgsGeometry(geometry)
            results.append((result, dict(attributes)))
        self.work_area = math.fsum(geometry.area() for geometry, _ in polygons)
        self.exclusion_area = math.fsum(geometry.area() for geometry, _ in accepted)
        self.application_area = math.fsum(geometry.area() for geometry, _ in results)
        for geometry, attributes in results:
            attributes["面積ha"] = float(self.hectares(geometry.area(), 5))
            attributes["申請ha"] = float(self.hectares(self.application_area, 2))
            if not single:
                attributes["更新ha"] = float(self.hectares(self.work_area, 2))
        self.work_terms = [(attributes["label"], geometry.area()) for geometry, attributes in polygons]
        self.exclusion_terms = [(attributes["calculation_label"], geometry.area()) for geometry, attributes in accepted]
        self.original_layer = self.memory_polygon_layer("元区域" if single else "更新区域（除去前）", [
            (geometry, {**attributes, "area_m2": geometry.area()}) for geometry, attributes in polygons
        ])
        self.exclusion_layer = self.memory_polygon_layer("除地（採用部分）", accepted)
        definitions = LINE_ATTRIBUTE_DEFINITIONS if single else ATTRIBUTE_DEFINITIONS
        self.application_layer = self.memory_polygon_layer("区域" if single else "申請区域（除去後）", results, definitions)
        self.result_layers = [self.original_layer, self.exclusion_layer, self.application_layer]
        if self.reference_layer is not None:
            self.result_layers.append(self.reference_layer)
        self.apply_polygon_style(self.application_layer, "polygon.qml")
        self.apply_polygon_style(self.exclusion_layer, "exclusion_polygon.qml")

    def calculate_reference_points(self):
        self.reference_layer = None
        self.reference_distance = None
        source = self.kijunten.currentLayer()
        if source is None:
            return
        if not source.isValid() or source.geometryType() != QgsWkbTypes.PointGeometry:
            raise ValueError("基準点のポイントレイヤを指定してください（2点必要です）")
        text = self.kijuntenExp.expression().strip()
        # QGISの真偽値変換を使う。Pythonのbool('false')等とは区別する。
        expression = QgsExpression(f"CASE WHEN ({text}) THEN TRUE ELSE FALSE END") if text else None
        context = QgsExpressionContext()
        context.appendScopes(QgsExpressionContextUtils.globalProjectLayerScopes(source))
        if expression and (expression.hasParserError() or not expression.prepare(context)):
            raise ValueError(f"基準点指定式: {expression.parserErrorString() or expression.evalErrorString()}")
        points = []
        for feature in source.getFeatures():
            if expression:
                context.setFeature(feature)
                selected = expression.evaluate(context)
                if expression.hasEvalError():
                    raise ValueError(f"基準点 地物ID={feature.id()}: {expression.evalErrorString()}")
                if not selected:
                    continue
            geometry = self.checked_geometry(source, feature)
            parts = geometry.asMultiPoint() if geometry.isMultipart() else [geometry.asPoint()]
            points.extend((point, feature.id()) for point in parts)
            if len(points) > 2:
                raise ValueError("基準点が3点以上選択されています。指定式で2点に絞ってください")
        if len(points) != 2:
            raise ValueError(f"基準点は2点必要です（選択された点数: {len(points)}）")
        self.reference_distance = math.hypot(points[1][0].x() - points[0][0].x(),
                                             points[1][0].y() - points[0][0].y())
        minimum = self.minKijuntenkan.value()
        if self.reference_distance < minimum:
            raise ValueError(f"基準点間距離 {self.reference_distance:.6f} m は、"
                             f"最小基準点間距離 {minimum:g} m 未満です")
        layer = QgsVectorLayer(f"Point?crs={self.crs.crs().authid()}", "基準点", "memory")
        layer.dataProvider().addAttributes([QgsField("source_id", QVariant.LongLong)])
        layer.updateFields()
        for point, source_id in points:
            feature = QgsFeature(layer.fields())
            feature.setGeometry(QgsGeometry.fromPointXY(point))
            feature.setAttributes([source_id])
            if not layer.dataProvider().addFeature(feature):
                raise ValueError("基準点を作成できません")
        symbol = QgsMarkerSymbol.createSimple({"name": "circle", "color": "255,0,0,255",
                                                "outline_style": "no", "size": "2"})
        symbol.setSizeUnit(Qgis.RenderUnit.Millimeters)
        layer.setRenderer(QgsSingleSymbolRenderer(symbol))
        layer.setLabelsEnabled(False)
        layer.updateExtents()
        self.reference_layer = layer

    def apply_polygon_style(self, layer, filename):
        is_line = layer.geometryType() == QgsWkbTypes.LineGeometry
        if is_line:
            layer.setRenderer(QgsSingleSymbolRenderer(QgsLineSymbol.createSimple({
                "line_color": "227,26,28,255", "line_width": "0.53", "line_width_unit": "MM",
                "capstyle": "flat", "joinstyle": "miter",
            })))
        else:
            _, ok = layer.loadNamedStyle(str(ROOT / "styles" / filename))
            if not ok:
                raise ValueError(f"スタイルを読み込めません: {filename}")
        labeling = layer.labeling()
        settings = labeling.settings() if labeling else QgsPalLayerSettings()
        settings.fieldName = "label"
        settings.isExpression = False
        settings.geometryGeneratorEnabled = False
        settings.placement = Qgis.LabelPlacement.Line if is_line else Qgis.LabelPlacement.OutsidePolygons
        settings.dist = 1.5
        settings.distUnits = Qgis.RenderUnit.Millimeters
        settings.obstacleType = QgsPalLayerSettings.ObstacleType.PolygonBoundary
        if settings.callout():
            settings.callout().setEnabled(False)
        placement = settings.placementSettings()
        placement.setOverlapHandling(Qgis.LabelOverlapHandling.PreventOverlap)
        placement.setAllowDegradedPlacement(True)
        settings.setPlacementSettings(placement)
        text_format = settings.format()
        label_font = QFont("Yu Gothic")
        label_font.setBold(True)
        text_format.setFont(label_font)
        text_format.setSize(9)
        text_format.setSizeUnit(Qgis.RenderUnit.Points)
        text_format.setColor(QColor("black"))
        buffer = text_format.buffer()
        buffer.setEnabled(True)
        buffer.setColor(QColor("white"))
        buffer.setSize(0.5)
        buffer.setSizeUnit(Qgis.RenderUnit.Millimeters)
        text_format.setBuffer(buffer)
        settings.setFormat(text_format)
        layer.setLabeling(QgsVectorLayerSimpleLabeling(settings))
        layer.setLabelsEnabled(any(self.clean_html_text(feature["label"]).strip() for feature in layer.getFeatures()))

    def log_calculation(self):
        if self.reference_layer is not None:
            self.append_output_log(f"基準点: 2点 / 点間距離: {self.reference_distance:.6f} m")
        if self._current_drawing["kind"] != "申請区域":
            attributes = self._current_drawing["attributes"]
            self.append_output_log(f"延長: {attributes['延長m']} m / 幅: {attributes['幅m']:.2f} m")
            return
        for title, terms, total in (("更新面積", self.work_terms, self.work_area),
                                    ("除地", self.exclusion_terms, self.exclusion_area)):
            for name, area in terms:
                self.append_output_log(f"{title}: {name + ' ' if name else ''}{area:.8f} m²")
            self.append_output_log(f"{title}合計: {total:.8f} m² / {self.hectares(total, 2)} ha")
        self.append_output_log(f"申請面積: {self.application_area:.8f} m² / {self.hectares(self.application_area, 2)} ha")
        for name, area in self.excluded_small_parts:
            self.append_output_log(f"最小除地面積未満で対象外: {name + ' ' if name else ''}{area:.8f} m²")

    def paper_factor(self):
        return 420 / 297 if self.paper.currentText() == "A3" else 1.0

    def create_uav_layout(self, layers):
        size = 150 * self.paper_factor()
        layout = QgsPrintLayout(QgsProject.instance())
        layout.initializeDefaults()
        layout.pageCollection().page(0).setPageSize(QgsLayoutSize(size, size))
        map_item = QgsLayoutItemMap(layout)
        layout.addLayoutItem(map_item)
        map_item.attemptResize(QgsLayoutSize(size, size))
        map_item.attemptMove(QgsLayoutPoint(0, 0))
        self.set_map_extent(map_item, layers)
        layout.renderContext().setDpi(300)
        return layout

    def set_map_extent(self, map_item, layers, scale=None, extent=None):
        map_item.setCrs(self.crs.crs())
        map_item.setLayers(layers)
        map_item.setKeepLayerSet(True)
        if extent is None:
            ortho = self.olso.currentLayer()
            if ortho is not None and any(layer.type() == ortho.type() for layer in layers):
                extent = QgsCoordinateTransform(ortho.crs(), self.crs.crs(), QgsProject.instance()).transformBoundingBox(ortho.extent())
            else:
                extent = self.original_layer.extent()
                if self.reference_layer is not None and self.reference_layer in layers:
                    extent.combineExtentWith(self.reference_layer.extent())
        if extent.isEmpty():
            if max(extent.width(), extent.height()) > 0:
                extent.grow(max(extent.width(), extent.height()) * 0.001)
            else:
                raise ValueError("対象地物の表示範囲を取得できません")
        map_item.zoomToExtent(extent)
        map_item.setScale(self.scale.scale() if scale is None else scale)
        frame_extent = map_item.extent()
        if self.reference_layer is not None and self.reference_layer in layers and any(
                not frame_extent.contains(feature.geometry().asPoint())
                for feature in self.reference_layer.getFeatures()):
            raise ValueError("指定縮尺では基準点が地図枠の外になります。縮尺の分母を大きくしてください")
        if not frame_extent.contains(extent):
            self.append_output_log("指定縮尺では区域の一部が地図枠の外になります。縮尺の分母を大きくしてください。")

    def drawing_output_dir(self):
        return self._drawing_output_dir

    def shared_output_dir(self):
        return self.output_dir() / "asset"

    def input_source_output_layers(self):
        """Preserve selected input polygons and centerlines with their original attributes."""
        layers = []
        for combo_name, filter_name, title in (
                ("polygon", "polygonFilter", "施行地（入力ポリゴン）"),
                ("jochiPolygon", "jochiFilter", "除地（入力ポリゴン）"),
                ("sagyodoLine", "sagyodoFilter", "作業道（入力ライン）"),
                ("singleLine", "singleLineFilter", "付帯作工物（入力ライン）")):
            source = getattr(self, combo_name).currentLayer()
            if source is None:
                continue
            geometry_type = "MultiPolygon" if source.geometryType() == QgsWkbTypes.PolygonGeometry else "MultiLineString"
            layer = QgsVectorLayer(f"{geometry_type}?crs={self.crs.crs().authid()}", title, "memory")
            fields = list(source.fields())
            if combo_name == "sagyodoLine":
                length_index = source.fields().indexFromName("延長m")
                length_field = QgsField("延長m", QVariant.LongLong)
                if length_index >= 0:
                    fields[length_index] = length_field
                else:
                    fields.append(length_field)
            layer.dataProvider().addAttributes(fields)
            layer.updateFields()
            for feature in self.selected_features(combo_name, filter_name):
                geometry = self.checked_geometry(source, feature)
                geometry.convertToMultiType()
                copied = QgsFeature(layer.fields())
                copied.setGeometry(geometry)
                values = feature.attributes()
                if combo_name == "sagyodoLine":
                    length = math.floor(geometry.length())
                    if length_index >= 0:
                        values[length_index] = length
                    else:
                        values.append(length)
                copied.setAttributes(values)
                if not layer.dataProvider().addFeature(copied):
                    raise ValueError(f"{title}: 地物を保存できません")
            for index in range(len(source.fields())):
                layer.setFieldAlias(index, source.attributeAlias(index))
            if source.renderer():
                layer.setRenderer(source.renderer().clone())
            layer.updateExtents()
            layers.append(layer)
        return layers

    def export_uav(self):
        output = self.output_dir()
        create_project = self.outputMode.currentIndex() == 0
        shutil.copytree(ROOT / "html_shinsoku" / "asset", output / "asset", dirs_exist_ok=True)
        (output / "backup").mkdir(exist_ok=True)
        if create_project:
            self.shared_output_dir().mkdir(exist_ok=True)
        sheets = []
        copied_ortho = self.copy_assignment_ortho()
        self._editable_projects = (UnifiedProjects(self, output, getattr(self, "_project_snapshot", None), copied_ortho)
                                   if create_project else None)
        for index, drawing in enumerate(self.drawings, 1):
            self.activate_drawing(drawing)
            self._drawing_output_dir = output / drawing["folder"]
            self._drawing_output_dir.mkdir(parents=True)
            ortho = self.olso.currentLayer()
            if ortho is not None and copied_ortho is not None and (
                    Path(ortho.source().split('|')[0]).resolve()
                    == Path(self.assignmentOlso.filePath()).resolve()):
                ortho = ortho.clone()
                ortho.setDataSource(str(copied_ortho), ortho.name(), ortho.providerType())
                if not ortho.isValid():
                    raise OSError("コピーしたオルソ画像を開けません")
            layers = [self.application_layer]
            if self.reference_layer is not None:
                layers.insert(0, self.reference_layer)
            if ortho is not None:
                layers.append(ortho)
            layout = self.create_uav_layout(layers)
            map_path = output / "asset" / f"drawing_{index}_map.png"
            if not create_project:
                with self.export_layer_context(layout):
                    image = QgsLayoutExporter(layout).renderPageToImage(0, dpi=300)
                if image.isNull() or not image.save(str(map_path), "PNG"):
                    raise OSError("地図PNGの出力に失敗しました")
                self._export_map_scale = self.layout_display_scale(layout)
                sheets.append(self.build_uav_html_sheet(index, map_path.name))
                self.export_shapefile()
                continue
            captured = []
            for layer in layers:
                key = ("output:references" if layer is self.reference_layer else
                       "output:" + drawing["key"] if layer is self.application_layer else "input:" + source_key(self.olso.currentLayer()))
                self._editable_projects.add_dataset(key, layer)
                style = QgsMapLayerStyle()
                style.readFromLayer(layer)
                captured.append((layer.id(), key, style))
            state = {key: value for key, value in drawing.items()
                     if key not in ("original_layer", "exclusion_layer", "application_layer", "result_layers", "reference_layer")}
            self._editable_projects.capture(layout, drawing["key"], drawing["folder"],
                                            f"asset/{map_path.name}", captured, state)
            self.export_shapefile()
        self.activate_drawing(self.drawings[0])
        if self.isIchizu.isChecked():
            self.export_location()
        if not create_project:
            self.write_uav_html(sheets)
            self.append_output_log("QGZ・GPKGは作成・更新せず、入力から再計算して出力しました。")
            return
        project, info = self._editable_projects.save()
        by_key = {drawing["key"]: drawing for drawing in self.drawings}
        for item in info["layouts"]:
            layout = project.layoutManager().layoutByName(item["layout"])
            target = output / item["output"]
            target.parent.mkdir(parents=True, exist_ok=True)
            if item["drawing"] is None:
                if QgsLayoutExporter(layout).exportToPdf(str(target), QgsLayoutExporter.PdfExportSettings()) != QgsLayoutExporter.Success:
                    raise OSError("位置図PDFの出力に失敗しました")
            else:
                with self.export_layer_context(layout):
                    image = QgsLayoutExporter(layout).renderPageToImage(0, dpi=300)
                if image.isNull() or not image.save(str(target), "PNG"):
                    raise OSError("地図PNGの出力に失敗しました")
                self.activate_drawing(by_key[item["key"]])
                self._export_map_scale = self.layout_display_scale(layout)
                sheets.append(self.build_uav_html_sheet(len(sheets) + 1, target.name))
        self.write_uav_html(sheets)
        self.activate_drawing(self.drawings[0])

    @staticmethod
    def ortho_area_name(records):
        compartments = {}
        for attributes in records:
            numbers = [None if QgsVariantUtils.isNull(attributes[field]) else int(attributes[field])
                       for field in ("林班", "小班")]
            if numbers == [None, None]:
                continue
            compartments.setdefault(numbers[0], set()).add(numbers[1])
        names = []
        for rinpan, shohans in sorted(compartments.items(), key=lambda item: -1 if item[0] is None else item[0]):
            name = f"{rinpan}林班" if rinpan is not None else ""
            small = sorted(value for value in shohans if value is not None)
            if small:
                name += "・".join(map(str, small)) + "小班"
            if name:
                names.append(name)
        return "_".join(names)

    def compartment_file_stem(self):
        records = [feature for drawing in self.drawings for feature in drawing["application_layer"].getFeatures()]
        area = self.ortho_area_name(records)
        return self.safe_file_name(area, max_length=None) if area else ""

    def project_file_name(self):
        return (self.compartment_file_stem() or "未指定") + ".qgz"

    def ortho_file_stem(self):
        area = self.compartment_file_stem()
        return self.safe_file_name(area + " - オルソ" if area else "オルソ", max_length=None)

    def copy_assignment_ortho(self):
        raw = self.assignmentOlso.filePath().strip()
        if not raw:
            return
        image = Path(raw)
        final = Path(getattr(self, "_final_output_dir", self.output_dir())).resolve()
        filename = self.ortho_file_stem() + image.suffix.lower()
        existing = final / "オルソ" / filename
        if self.outputMode.currentIndex() == 1 and image.resolve().parent == (final / "オルソ").resolve() and image.is_file():
            # The open project may hold the copied raster. Reuse it without a file swap.
            retained = getattr(self, "_retained_output_paths", set())
            retained.update(path.relative_to(final) for path in image.parent.glob(image.stem + ".*") if path.is_file())
            self._retained_output_paths = retained
            if image.resolve() == existing.resolve():
                return image
        directory = self.output_dir() / "オルソ"
        directory.mkdir(exist_ok=True)
        target = directory / filename
        shutil.copy2(image, target)
        for suffix in (".tfw", ".tifw", ".jgw", ".jpgw", ".pgw", ".pngw", ".wld", ".prj"):
            sidecar = image.with_suffix(suffix)
            if sidecar.is_file():
                shutil.copy2(sidecar, target.with_suffix(suffix))
        for suffix in (".aux.xml", ".ovr"):
            sidecar = Path(str(image) + suffix)
            if sidecar.is_file():
                shutil.copy2(sidecar, Path(str(target) + suffix))
        self.append_output_log(f"オルソ画像: {self.displayed_output_path(target)}")
        return target

    def export_shapefile(self):
        directory = self.drawing_output_dir() / "shp"
        directory.mkdir(parents=True, exist_ok=True)
        kind = self._current_drawing['kind']
        filename = f"{self.safe_file_name(self._current_drawing['title'])} - {kind}.shp"
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "ESRI Shapefile"
        options.fileEncoding = "UTF-8"
        definitions = list(ATTRIBUTE_DEFINITIONS)
        if self._current_drawing["kind"] != "申請区域":
            definitions = list(LINE_ATTRIBUTE_DEFINITIONS)
        options.attributes = [self.application_layer.fields().indexFromName(field)
                              for field, _, _, _, _ in definitions]
        options.overrideGeometryType = (QgsWkbTypes.MultiLineString
                                        if self.application_layer.geometryType() == QgsWkbTypes.LineGeometry
                                        else QgsWkbTypes.MultiPolygon)
        options.forceMulti = True
        result = QgsVectorFileWriter.writeAsVectorFormatV3(
            self.application_layer, str(directory / filename),
            QgsProject.instance().transformContext(), options,
        )
        if result[0] != QgsVectorFileWriter.NoError:
            raise OSError(f"シェープファイル出力に失敗しました: {result[1]}")
        # DBFはフィールド名が10バイトまで。正式名をQGISの別名に保持する。
        exported = QgsVectorLayer(str(directory / filename), "申請区域", "ogr")
        exported.setRenderer(self.application_layer.renderer().clone())
        for field, alias, _, _, _ in definitions:
            index = exported.fields().indexFromName(field)
            if index < 0:
                raise OSError(f"シェープファイル属性を保存できません: {field}")
            exported.setFieldAlias(index, alias)
        exported.saveNamedStyle(str((directory / filename).with_suffix(".qml")))
        self.append_output_log(f"シェープファイル: {self.displayed_output_path(directory / filename)}")

    def export_location(self):
        document = QDomDocument()
        document.setContent((ROOT / "styles" / "location.qpt").read_text(encoding="utf-8"))
        layout = QgsPrintLayout(QgsProject.instance())
        layout.initializeDefaults()
        layout.loadFromTemplate(document, QgsReadWriteContext())
        a3 = self.locationPaper.currentText() == "A3"
        width, height = (297, 420) if a3 else (210, 297)
        if self.ichizuDirection.currentIndex() == 1:
            width, height = height, width
        layout.pageCollection().page(0).setPageSize(QgsLayoutSize(width, height))
        map_item = layout.itemById("地図 1")
        if not isinstance(map_item, QgsLayoutItemMap):
            raise ValueError("位置図テンプレートの地図枠がありません")
        map_item.attemptResize(QgsLayoutSize(width - 10, height - 10))
        map_item.attemptMove(QgsLayoutPoint(width / 2, height / 2), True)
        arrow = layout.itemById("方位記号")
        arrow.attemptMove(QgsLayoutPoint(174.863 + width - 210, 9.67), True)
        for name in ("縮尺", "位置図見出し", "スケールバー", "位置図タイトル", "帯背景"):
            item = layout.itemById(name)
            point = item.positionWithUnits()
            item.attemptMove(QgsLayoutPoint(point.x() + width - 210, point.y() + height - 297), True)
        self.set_location_picture_paths(layout, ROOT / "styles")
        if not self.set_location_label_text(layout):
            raise ValueError("位置図タイトルを設定できません")
        # Same final datasets; location-specific appearance is a named style only.
        location_layers = []
        captured = []
        combined_extent = None
        for drawing in self.drawings:
            layer = drawing["application_layer"].clone()
            symbol = (QgsLineSymbol.createSimple({"line_color": "0,80,255,255", "line_width": "0.265", "capstyle": "flat"})
                      if drawing["kind"] == "付帯作工物" else
                      QgsFillSymbol.createSimple({"style": "no", "outline_color": "227,26,28,255", "outline_width": "0.53"}))
            layer.setRenderer(QgsSingleSymbolRenderer(symbol))
            layer.setLabelsEnabled(False)
            if layer.featureCount():
                location_layers.insert(0, layer)
                if combined_extent is None:
                    combined_extent = layer.extent()
                else:
                    combined_extent.combineExtentWith(layer.extent())
                style = QgsMapLayerStyle()
                style.readFromLayer(layer)
                captured.insert(0, (layer.id(), "output:" + drawing["key"], style))
        inputs = [getattr(self, name).currentLayer() for name in
                  ("polygon", "jochiPolygon", "sagyodoLine", "kijunten", "singleLine", "olso")]
        generated = [layer for drawing in self.drawings for layer in drawing["result_layers"]]
        canvas = list(self.iface.mapCanvas().layers()) if self.iface else []
        saved_datasets = (metadata(QgsProject.instance()) or {}).get("datasets", {})
        managed = {layer_id for key, layer_id in saved_datasets.items() if not key.startswith("background:")}
        for key, layer_id in saved_datasets.items():
            if key.startswith("background:"):
                layer = QgsProject.instance().mapLayer(layer_id)
                if layer is not None and layer not in canvas:
                    canvas.append(layer)
        backgrounds = [layer for layer in canvas if layer not in inputs and layer not in generated
                       and layer.providerType() != "memory" and layer.id() not in managed
                       and (not isinstance(layer, QgsVectorLayer) or layer.featureCount() != 0)]
        # ファイル選択で指定されたコピー用オルソも位置図には含めない。
        assigned = self.assignmentOlso.filePath().strip()
        if assigned:
            backgrounds = [layer for layer in backgrounds
                           if Path(layer.source().split("|")[0]).resolve() != Path(assigned).resolve()]
        for layer in backgrounds:
            key = "background:" + source_key(layer)
            if self._editable_projects is not None:
                self._editable_projects.add_dataset(key, layer)
            style = QgsMapLayerStyle()
            style.readFromLayer(layer)
            captured.append((layer.id(), key, style))
        self.set_map_extent(map_item, location_layers + backgrounds,
                            scale=self.locationScale.scale(), extent=combined_extent)
        if not self.set_location_scale_bars(layout, map_item):
            raise ValueError("位置図の縮尺を設定できません")
        directory = self.output_dir() / "位置図"
        directory.mkdir(exist_ok=True)
        path = directory / "位置図.pdf"
        if self._editable_projects is not None:
            self._editable_projects.capture(layout, "location", "位置図", path.relative_to(self.output_dir()).as_posix(), captured)
        elif QgsLayoutExporter(layout).exportToPdf(str(path), QgsLayoutExporter.PdfExportSettings()) != QgsLayoutExporter.Success:
            raise OSError("位置図PDFの出力に失敗しました")

    def area_quantity(self, value):
        text = self.format_decimal(value, self.areaDecimals.value())
        return f"{text} m²", rf"{text}\,\mathrm{{m}}^{{2}}"

    def area_sum_parts(self, terms, total):
        if not terms:
            return [self.area_quantity(total)]
        parts = []
        for index, (name, area) in enumerate(terms):
            if index:
                parts.append(("+", "+"))
            plain, latex = self.area_quantity(area)
            if name:
                plain, latex = f"{plain}（{name}）", rf"{latex}\,{self.latex_text('（' + name + '）')}"
            parts.append((plain, latex))
        if len(terms) > 1:
            displayed_sum = sum(Decimal(self.format_decimal(area, self.areaDecimals.value())) for _, area in terms)
            total_display = Decimal(self.format_decimal(total, self.areaDecimals.value()))
            parts.append(("=", "=") if displayed_sum == total_display else ("≃", r"\simeq"))
            parts.append(self.area_quantity(total))
        return parts

    def build_uav_html_sheet(self, index, map_filename):
        displayed_scale = getattr(self, "_export_map_scale", self.scale.scale())
        root = ET.parse(str(ROOT / "html_shinsoku" / "index.html"), ET.HTMLParser()).getroot()
        if self.reference_layer is None:
            for legend in root.xpath("//*[@id='map_legend']"):
                legend.getparent().remove(legend)
        metadata = self.drawing_metadata()
        for name, text in {
            "seizubi": metadata["date"],
            "seizusha": metadata["draftsperson"], "seizujigyosha": metadata["company"],
            "drawing_name": metadata["name"], "sanrinshoyusha": metadata["owner"],
            "shinseino": metadata["application_no"], "crs": self.format_crs_display(self.crs.crs()),
            "scale": f"1:{displayed_scale:g}",
        }.items():
            for element in root.xpath(f"//*[@id='{name}']"):
                if name in {"crs", "scale"}:
                    element.text = text
                else:
                    element.text = None
                    element.set("title", text)
                    ET.SubElement(element, "span", {"class": "header-text"}).text = text
        for element in root.xpath("//*[@id='scale']"):
            self.apply_latex_parts(element, [
                (f"1:{displayed_scale:g}", rf"1\mathbin{{:}}{displayed_scale:g}"),
            ])
        panel = root.xpath("//*[@id='calc_area']")[0]
        self.replace_children_with_text(panel, "")
        single = self._current_drawing["kind"] != "申請区域"
        if single:
            root.xpath("//h1")[0].text = "付帯作工物図"
            root.xpath("//div[@class='calc-heading']")[0].text = "延長"
            attributes = self._current_drawing["attributes"]
            length = attributes["延長m"]
            width = f"{attributes['幅m']:.2f}"
            rows = [
                ("地物名", [(attributes["label"], self.latex_text(attributes["label"]))]),
                ("幅", [(f"{width} m", rf"{width}\,\mathrm{{m}}")]),
                ("延長", [(f"{length} m", rf"\color{{red}}{{{length}\,\mathrm{{m}}}}")]),
            ]
        elif self.isJochikeisan.isChecked():
            rows = [("更新面積", self.area_sum_parts(self.work_terms, self.work_area)),
                    ("除地", self.area_sum_parts(self.exclusion_terms, self.exclusion_area))]
            work = self.area_quantity(self.work_area)
            excluded = self.area_quantity(self.exclusion_area)
            application = self.area_quantity(self.application_area)
            displayed_difference = Decimal(self.format_decimal(self.work_area, self.areaDecimals.value())) - Decimal(self.format_decimal(self.exclusion_area, self.areaDecimals.value()))
            displayed_result = Decimal(self.format_decimal(self.application_area, self.areaDecimals.value()))
            symbol = ("=", "=") if displayed_difference == displayed_result else ("≃", r"\simeq")
            ha = (Decimal(str(self.application_area)) / Decimal("10000"))
            truncated = self.hectares(self.application_area, self.haDecimals.value())
            # 換算左辺の表示値も考慮し、丸めたm²とhaが違えば近似記号にする。
            exact = ha == truncated and displayed_result / Decimal("10000") == truncated
            final_symbol = ("=", "=") if exact else ("≃", r"\simeq")
            rows.append(("申請面積", [work, ("−", "-"), excluded, symbol, application,
                                      final_symbol, (f"{truncated} ha", rf"\color{{red}}{{{truncated}\,\mathrm{{ha}}}}")]))
        else:
            rows = [("更新面積", [(f"{self.hectares(self.work_area, 2)} ha", rf"{self.hectares(self.work_area, 2)}\,\mathrm{{ha}}")]),
                    ("申請面積", [(f"{self.hectares(self.application_area, 2)} ha", rf"\color{{red}}{{{self.hectares(self.application_area, 2)}\,\mathrm{{ha}}}}")])]
        for title, parts in rows:
            is_application = title in ("申請面積", "面積")
            row = ET.SubElement(panel, "div", {"class": "calc-row calc-application" if is_application else "calc-row"})
            ET.SubElement(row, "div", {"class": "calc-label"}).text = title
            formula = ET.SubElement(row, "div", {"class": "calc-formula"})
            if is_application:
                self.apply_latex_parts(formula, parts[:-2] if self.isJochikeisan.isChecked() else [])
                result = ET.SubElement(row, "div", {"class": "calc-result"})
                self.apply_latex_parts(result, parts[-2:] if self.isJochikeisan.isChecked() else parts)
                ET.SubElement(row, "div", {"class": "calc-result-caption"}).text = "ヘクタール換算"
            elif title == "除地":
                self.render_deduction_terms(formula)
            else:
                if single and title == "延長":
                    formula.set("class", "calc-formula calc-result")
                self.apply_latex_parts(formula, parts)
        sheet = root.xpath("//*[@id='main_container']")[0]
        sheet.set("class", "drawing-sheet")
        sheet.set("data-drawing", str(index))
        sheet.set("data-title", self._current_drawing["folder"])
        sheet.xpath(".//img[contains(@class, 'main_map')]")[0].set("src", f"asset/{map_filename}")
        # Classes keep the shared CSS; unique IDs prevent cross-drawing controls.
        for element in sheet.iter():
            if element.get("id"):
                original_id = element.get("id")
                element.set("class", (element.get("class", "") + " " + original_id).strip())
                element.set("id", f"drawing_{index}_{original_id}")
        return sheet

    def render_deduction_terms(self, formula):
        formula.set("class", "calc-formula calc-deduction")
        terms = self.exclusion_terms or [("", self.exclusion_area)]
        for index, (name, area) in enumerate(terms):
            line = ET.SubElement(formula, "div", {"class": "deduction-term"})
            operator = ET.SubElement(line, "span", {"class": "deduction-operator"})
            self.apply_latex_parts(operator, [("+", "+")] if index else [])
            quantity = ET.SubElement(line, "span", {"class": "deduction-quantity"})
            self.apply_latex_parts(quantity, [self.area_quantity(area)])
            description = ET.SubElement(line, "span", {"class": "deduction-description"})
            if name:
                chunks = name.replace(" × ", "・× ").split("・")
                for chunk_index, chunk in enumerate(chunks):
                    token = ET.SubElement(description, "span", {"class": "deduction-description-token"})
                    token.text = ("（" if chunk_index == 0 else "") + chunk
                    token.text += "）" if chunk_index == len(chunks) - 1 else (" " if chunks[chunk_index + 1].startswith("× ") else "・")
        if len(terms) > 1:
            displayed_sum = sum(Decimal(self.format_decimal(area, self.areaDecimals.value())) for _, area in terms)
            total = Decimal(self.format_decimal(self.exclusion_area, self.areaDecimals.value()))
            symbol = ("=", "=") if displayed_sum == total else ("≃", r"\simeq")
            line = ET.SubElement(formula, "div", {"class": "deduction-total"})
            self.apply_latex_parts(line, [symbol, self.area_quantity(self.exclusion_area)])

    def write_uav_html(self, sheets):
        root = ET.parse(str(ROOT / "html_shinsoku" / "index.html"), ET.HTMLParser()).getroot()
        body = root.find("body")
        body.remove(root.xpath("//*[@id='main_container']")[0])
        for index, sheet in enumerate(sheets):
            body.insert(index, sheet)
        root.find("head/title").text = "申請区域図・付帯作工物図"
        body.set("data-paper", self.paper.currentText())
        width, height = (420, 297) if self.paper.currentText() == "A3" else (297, 210)
        style = ET.SubElement(root.find("head"), "style")
        style.text = (f":root{{--paper-factor:{self.paper_factor()};--page-width:{width}mm;--page-height:{height}mm;}}"
                      f"@page{{size:{self.paper.currentText()} landscape;margin:0;}}")
        path = self.output_dir() / "index.html"
        path.write_text("<!DOCTYPE html>\n" + ET.tostring(root, encoding="unicode", method="html"), encoding="utf-8")
        self.append_output_log(f"申請区域図（{len(sheets)}図面）: {self.displayed_output_path(path)}")
