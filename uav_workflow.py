"""UAV geometry, application attributes, and drawing output."""
import math
import os
import shutil
import tempfile
import zipfile
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
    QgsPrintLayout, QgsProject, QgsReadWriteContext, QgsVectorFileWriter,
    QgsLineSymbol, QgsMarkerSymbol, QgsSingleSymbolRenderer,
    QgsVariantUtils, QgsVectorLayer, QgsVectorLayerSimpleLabeling, QgsWkbTypes,
)

from .editable_projects import EditableProjects


ROOT = Path(__file__).parent
EXPRESSION_NAMES = (
    "polygonName", "shichoson", "rinpan", "shohan", "edaban",
    "seizubi2", "seizusha2", "sanrinshoyusha2", "jochiName", "jochiNameSagyodo", "kijuntenExp",
)
TEXT_NAMES = (
    "seizujigyosha", "seizusha", "rinshohan", "sanrinshoyusha",
    "shinseibango", "jigyoCode",
)
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


class UavWorkflow:
    def setup_uav_inputs(self):
        for name, layer_filter in (
            ("polygon", QgsMapLayerProxyModel.PolygonLayer),
            ("jochiPolygon", QgsMapLayerProxyModel.PolygonLayer),
            ("sagyodoLine", QgsMapLayerProxyModel.LineLayer),
            ("olso", QgsMapLayerProxyModel.RasterLayer),
            ("kijunten", QgsMapLayerProxyModel.PointLayer),
        ):
            combo = getattr(self, name)
            combo.setFilters(layer_filter)
            combo.setAllowEmptyLayer(True)
            combo.setLayer(None)
        for combo_name, expression_names in (
            ("polygon", EXPRESSION_NAMES[:8]),
            ("jochiPolygon", ("jochiName",)),
            ("sagyodoLine", ("jochiNameSagyodo",)),
            ("kijunten", ("kijuntenExp",)),
        ):
            combo = getattr(self, combo_name)
            for name in expression_names:
                expression_widget = getattr(self, name)
                try:
                    combo.layerChanged.disconnect(expression_widget.setLayer)
                except TypeError:
                    pass
                expression_widget.setLayer(combo.currentLayer())
                expression_widget.setExpression("")
                combo.layerChanged.connect(
                    lambda layer, field=expression_widget: self.bind_expression_layer(field, layer)
                )
        self.sagyodoLine.layerChanged.connect(
            lambda layer: self.hukuin.setEnabled(layer is not None)
        )
        self.hukuin.setEnabled(False)
        self.hukuin.setMaximum(10000)
        self.hukuin.setDecimals(3)
        self.haDecimals.setValue(2)
        self.scale.setScale(5000)
        self.locationScale.setScale(5000)
        self.locationScale.setEnabled(self.isIchizu.isChecked())
        self.isIchizu.toggled.connect(self.locationScale.setEnabled)
        self.assignmentOlso.setFilter("オルソ画像 (*.tif *.tiff *.jpg *.jpeg *.png *.jp2 *.ecw *.img)")
        self.assignmentOlso.setEnabled(self.makeAssignment.isChecked())
        self.result_layers = []

    @staticmethod
    def bind_expression_layer(widget, layer):
        # setLayerの自動選択で、任意の空欄を最初の属性名へ置き換えない。
        expression = widget.expression()
        widget.setLayer(layer)
        widget.setExpression(expression)

    def configuration_data(self):
        return {
            "format": "RingyoZumenMaker.config", "version": 3, "mode": "uav",
            "basic": {**{name: getattr(self, name).text() for name in TEXT_NAMES},
                      "seizubi": self.seizubi.date().toString("yyyy-MM-dd")},
            "map": {
                "crs": self.crs.crs().authid(), "scale": self.scale.scale(),
                "paper": self.paper.currentText(),
                "show_deduction": self.isJochikeisan.isChecked(),
                "area_display_decimals": self.areaDecimals.value(),
                "hectare_display_decimals": self.haDecimals.value(),
                "create_location_map": self.isIchizu.isChecked(),
                "location_direction": self.ichizuDirection.currentIndex(),
                "location_scale": self.locationScale.scale(),
                "minimum_exclusion_area_a": self.minJochi.value(),
                "road_width_m": self.hukuin.value(),
                "minimum_reference_distance_m": self.minKijuntenkan.value(),
            },
            "layers": {name: self.layer_reference(getattr(self, name).currentLayer())
                       for name in ("polygon", "jochiPolygon", "sagyodoLine", "olso", "kijunten")},
            "expressions": {name: getattr(self, name).expression() for name in EXPRESSION_NAMES},
            "region": self.shinkokyoku.currentText(),
            "output": {
                "directory": self.fileName.filePath(),
                "backup_qgz": self.backupQgz.isChecked(),
                "config_save_mode": self.isSaveConfig.currentIndex(),
                "config_file": self.saveConfig.filePath(),
                "make_assignment": self.makeAssignment.isChecked(),
                "assignment_ortho": self.assignmentOlso.filePath(),
            },
        }

    def apply_configuration(self, data):
        basic = data.get("basic", {})
        legacy_names = {"seizujigyosha": "drawing_company", "seizusha": "draftsperson",
                        "rinshohan": "forest_compartment", "sanrinshoyusha": "forest_owner"}
        for name in TEXT_NAMES:
            getattr(self, name).setText(self.clean_html_text(
                basic.get(name, basic.get(legacy_names.get(name), ""))
            ))
        self.set_date_from_config(self.seizubi, basic.get("seizubi", basic.get("drawing_date")))
        settings = data.get("map", {})
        crs = QgsCoordinateReferenceSystem(settings.get("crs", ""))
        self.crs.setCrs(crs)
        self.scale.setScale(float(settings.get("scale") or 5000))
        self.locationScale.setScale(float(settings.get("location_scale", settings.get("scale") or 5000)))
        self.paper.setCurrentIndex(1 if settings.get("paper") == "A3" else 0)
        self.isJochikeisan.setChecked(bool(settings.get("show_deduction", False)))
        self.isIchizu.setChecked(bool(settings.get("create_location_map", False)))
        self.ichizuDirection.setCurrentIndex(1 if settings.get("location_direction") == 1 else 0)
        self.areaDecimals.setValue(int(settings.get("area_display_decimals", 0)))
        self.haDecimals.setValue(int(settings.get("hectare_display_decimals", 2)))
        self.minJochi.setValue(float(settings.get("minimum_exclusion_area_a", 1)))
        self.hukuin.setValue(float(settings.get("road_width_m", 0)))
        self.minKijuntenkan.setValue(int(settings.get("minimum_reference_distance_m", 20)))
        region_index = self.shinkokyoku.findText(str(data.get("region", "")))
        self.shinkokyoku.setCurrentIndex(max(0, region_index))
        missing = []
        for name in ("polygon", "jochiPolygon", "sagyodoLine", "olso", "kijunten"):
            reference = data.get("layers", {}).get(name)
            layer = self.resolve_layer_reference(reference)
            getattr(self, name).setLayer(layer)
            if reference and layer is None:
                missing.append(reference.get("name", name))
        for name in EXPRESSION_NAMES:
            getattr(self, name).setExpression(self.clean_html_text(data.get("expressions", {}).get(name)))
        output = data.get("output", {})
        self.fileName.setFilePath(self.clean_html_text(output.get("directory")))
        self.backupQgz.setChecked(bool(output.get("backup_qgz", False)))
        self.makeAssignment.setChecked(bool(output.get("make_assignment", False)))
        self.assignmentOlso.setFilePath(self.clean_html_text(output.get("assignment_ortho")))
        mode = int(output.get("config_save_mode", 0))
        self.isSaveConfig.setCurrentIndex(mode if mode in (0, 1, 2) else 0)
        self.saveConfig.setFilePath(self.clean_html_text(output.get("config_file")))
        self.update_save_config_enabled()
        return missing

    def validate_inputs(self):
        layer = self.polygon.currentLayer()
        if layer is None or not layer.isValid() or layer.featureCount() == 0:
            raise ValueError("地物のあるポリゴンレイヤを指定してください")
        crs = self.crs.crs()
        if not crs.isValid() or crs.isGeographic() or crs.mapUnits() != Qgis.DistanceUnit.Meters:
            raise ValueError("メートル単位の平面直角座標系などを指定してください")
        if not math.isfinite(self.scale.scale()) or self.scale.scale() <= 0:
            raise ValueError("有効な縮尺を指定してください")
        if self.isIchizu.isChecked() and (
                not math.isfinite(self.locationScale.scale()) or self.locationScale.scale() <= 0):
            raise ValueError("位置図の有効な縮尺を指定してください")
        if not self.fileName.filePath() or not Path(self.fileName.filePath()).is_dir():
            raise ValueError("出力先には存在するフォルダを指定してください")
        if self.sagyodoLine.currentLayer() and self.hukuin.value() <= 0:
            raise ValueError("作業道幅員は0より大きい値を指定してください")
        if self.isSaveConfig.currentIndex() == 2 and not self.saveConfig.filePath().strip():
            raise ValueError("設定保存ファイルを指定してください")
        if self.makeAssignment.isChecked():
            image = Path(self.assignmentOlso.filePath())
            if not self.assignmentOlso.filePath().strip() or not image.is_file():
                raise ValueError("提出用オルソ画像には存在する画像ファイルを指定してください")
            if image.suffix.lower() not in (".tif", ".tiff", ".jpg", ".jpeg", ".png", ".jp2", ".ecw", ".img"):
                raise ValueError("提出用オルソ画像の形式を確認してください")
        self.attribute_warnings = set()
        for name, label in (
            ("seizubi2", "製図年月日の個別指定（基本情報を継承）"),
            ("seizusha2", "製図者名の個別指定（基本情報を継承）"),
            ("sanrinshoyusha2", "森林所有者名の個別指定（基本情報を継承）"),
            ("shichoson", "市町村"), ("rinpan", "林班"), ("shohan", "小班"),
        ):
            if not getattr(self, name).expression().strip():
                self.attribute_warnings.add(label)
        for name, label in (("jigyoCode", "事業コード"), ("shinseibango", "申請番号")):
            if not getattr(self, name).text().strip():
                self.attribute_warnings.add(label)
        return True

    def on_submit(self, test=False):
        self.open_output_tab()
        self.clear_output_log()
        self.progressBar.setValue(0)
        staging = None
        try:
            self.validate_inputs()
            self.calculate_uav()
            if self.attribute_warnings:
                message = "次の項目は空欄です。継承または空欄のまま処理します:\n" + "\n".join(sorted(self.attribute_warnings))
                self.append_output_log(message)
                QMessageBox.warning(self, "入力項目の確認", message)
            self.log_calculation()
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
            if not self.commit_staged_output(staging, final, self.backupQgz.isChecked()):
                return
            if self.isSaveConfig.currentIndex() != 1 and not self.save_config_file():
                return
            self.append_output_log(f"出力を確定しました: {final}")
            self.progressBar.setValue(100)
        except Exception as error:
            self.append_output_log(f"エラー: {error}")
            QMessageBox.warning(self, "エラー", str(error))
            self.progressBar.setValue(0)
        finally:
            # QGZのクローンレイヤを解放してから一時フォルダを片づける。
            if hasattr(self, "_editable_projects"):
                self._editable_projects = None
            for attribute in ("_output_dir_override", "_final_output_dir"):
                if hasattr(self, attribute):
                    delattr(self, attribute)
            if staging is not None and staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

    def evaluate_attribute(self, widget_name, layer, feature, default=""):
        text = getattr(self, widget_name).expression().strip()
        if not text:
            return default
        expression = QgsExpression(text)
        if expression.hasParserError():
            raise ValueError(f"{widget_name}の式: {expression.parserErrorString()}")
        context = QgsExpressionContext()
        context.appendScopes(QgsExpressionContextUtils.globalProjectLayerScopes(layer))
        context.setFeature(feature)
        value = expression.evaluate(context)
        if expression.hasEvalError():
            raise ValueError(f"{layer.name()} 地物ID={feature.id()} / {widget_name}: {expression.evalErrorString()}")
        if value is None or QgsVariantUtils.isNull(value) or self.clean_html_text(value).strip() == "":
            if widget_name in ("shichoson", "rinpan", "shohan", "seizubi2", "seizusha2", "sanrinshoyusha2"):
                self.attribute_warnings.add(f"地物ID={feature.id()}: {widget_name}の評価結果が空欄")
            return default
        return value

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

    def polygon_attributes(self, layer, feature, name):
        values = {}
        for widget, field, default in (
            ("shichoson", "市町村", ""), ("rinpan", "林班", None),
            ("shohan", "小班", None), ("edaban", "枝番", ""),
            ("seizubi2", "製図日", self.seizubi.date()),
            ("seizusha2", "製図者", self.seizusha.text()),
            ("sanrinshoyusha2", "所有者", self.sanrinshoyusha.text()),
        ):
            value = self.evaluate_attribute(widget, layer, feature, default)
            if field in ("林班", "小班") and value is not None:
                try:
                    number = Decimal(str(value))
                    if number != number.to_integral_value():
                        raise ValueError()
                    value = int(number)
                    if not -2147483648 <= value <= 2147483647:
                        raise ValueError()
                except Exception:
                    raise ValueError(f"地物ID={feature.id()}: {field}は整数で指定してください")
            elif field == "製図日":
                if not isinstance(value, QDate):
                    value = QDate.fromString(str(value), "yyyy-MM-dd")
                if not value.isValid():
                    raise ValueError(f"地物ID={feature.id()}: 製図年月日は日付で指定してください")
            else:
                if value is not None:
                    value = self.clean_html_text(value)
            if value is None or (isinstance(value, str) and not value.strip()):
                if field not in ("枝番",):
                    self.attribute_warnings.add(f"地物ID={feature.id()}: {field}")
            values[field] = value
        values.update({"振興局": self.shinkokyoku.currentText(), "事業": self.jigyoCode.text(),
                       "申請No": self.shinseibango.text(), "label": name,
                       "source_id": feature.id()})
        return values

    def memory_polygon_layer(self, name, records, application_fields=False):
        layer = QgsVectorLayer(f"MultiPolygon?crs={self.crs.crs().authid()}", name, "memory")
        fields = [QgsField("label", QVariant.String, len=254), QgsField("source_id", QVariant.LongLong)]
        if application_fields:
            fields.extend(QgsField(field, kind, len=length, prec=precision)
                          for field, _, kind, length, precision in ATTRIBUTE_DEFINITIONS)
        else:
            fields.append(QgsField("area_m2", QVariant.Double))
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
        for field, alias, _, _, _ in ATTRIBUTE_DEFINITIONS:
            index = layer.fields().indexFromName(field)
            if index >= 0:
                layer.setFieldAlias(index, alias)
        layer.updateExtents()
        return layer

    def calculate_uav(self):
        self.calculate_reference_points()
        source = self.polygon.currentLayer()
        polygons = []
        for feature in source.getFeatures():
            geometry = self.checked_geometry(source, feature)
            name = self.clean_html_text(self.evaluate_attribute("polygonName", source, feature)).strip()
            polygons.append((geometry, self.polygon_attributes(source, feature, name)))
        exclusion_sources = []
        for combo, expression, is_road in (
            (self.jochiPolygon, "jochiName", False),
            (self.sagyodoLine, "jochiNameSagyodo", True),
        ):
            layer = combo.currentLayer()
            if layer is None:
                continue
            for feature in layer.getFeatures():
                geometry = self.checked_geometry(layer, feature)
                if is_road:
                    geometry = self.checked_operation(geometry.buffer(
                        self.hukuin.value() / 2, 1,
                        Qgis.EndCapStyle.Flat, Qgis.JoinStyle.Miter, 10,
                    ), "作業道バッファ")
                name = self.clean_html_text(self.evaluate_attribute(expression, layer, feature)).strip()
                description = "・".join(filter(None, (name, f"幅{self.hukuin.value():g}m"))) if is_road else name
                exclusion_sources.append((geometry, name, description))

        exclusions_union = self.checked_operation(
            QgsGeometry.unaryUnion([geometry for geometry, _, _ in exclusion_sources]),
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
                for exclusion_geometry, name, description in exclusion_sources:
                    if part.intersection(exclusion_geometry).area() > 1e-8:
                        if name and name not in names:
                            names.append(name)
                        if description and description not in descriptions:
                            descriptions.append(description)
                label = "・".join(names)
                # 閾値は表示丸め前の面積へ適用する。
                if part.area() < minimum:
                    self.excluded_small_parts.append((label, part.area()))
                    continue
                accepted_parts.append(part)
                accepted.append((part, {"label": label, "source_id": attributes["source_id"],
                                        "calculation_label": "・".join(descriptions),
                                        "area_m2": part.area()}))
            removed = QgsGeometry.unaryUnion(accepted_parts) if accepted_parts else QgsGeometry()
            result = self.checked_operation(geometry.difference(removed), "除地の除去") if accepted_parts else QgsGeometry(geometry)
            results.append((result, dict(attributes)))
        self.work_area = math.fsum(geometry.area() for geometry, _ in polygons)
        self.exclusion_area = math.fsum(geometry.area() for geometry, _ in accepted)
        self.application_area = math.fsum(geometry.area() for geometry, _ in results)
        for geometry, attributes in results:
            attributes.update({"面積ha": float(self.hectares(geometry.area(), 5)),
                               "申請ha": float(self.hectares(self.application_area, 2)),
                               "更新ha": float(self.hectares(self.work_area, 2))})
        self.work_terms = [(attributes["label"], geometry.area()) for geometry, attributes in polygons]
        self.exclusion_terms = [(attributes["calculation_label"], geometry.area()) for geometry, attributes in accepted]
        self.original_layer = self.memory_polygon_layer("更新区域（除去前）", [
            (geometry, {**attributes, "area_m2": geometry.area()}) for geometry, attributes in polygons
        ])
        self.exclusion_layer = self.memory_polygon_layer("除地（採用部分）", accepted)
        self.application_layer = self.memory_polygon_layer("申請区域（除去後）", results, True)
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
        if self.reference_distance < self.minKijuntenkan.value():
            raise ValueError(f"基準点間距離 {self.reference_distance:.6f} m は、"
                             f"最小基準点間距離 {self.minKijuntenkan.value()} m 未満です")
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
        _, ok = layer.loadNamedStyle(str(ROOT / "styles" / filename))
        if not ok:
            raise ValueError(f"スタイルを読み込めません: {filename}")
        labeling = layer.labeling()
        settings = labeling.settings() if labeling else QgsPalLayerSettings()
        settings.fieldName = "label"
        settings.isExpression = False
        settings.geometryGeneratorEnabled = False
        settings.placement = Qgis.LabelPlacement.OutsidePolygons
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

    def set_map_extent(self, map_item, layers, scale=None):
        map_item.setCrs(self.crs.crs())
        map_item.setLayers(layers)
        map_item.setKeepLayerSet(True)
        extent = self.original_layer.extent()
        if self.reference_layer is not None and self.reference_layer in layers:
            extent.combineExtentWith(self.reference_layer.extent())
        if extent.isEmpty():
            raise ValueError("ポリゴンの表示範囲を取得できません")
        map_item.zoomToExtent(extent)
        map_item.setScale(self.scale.scale() if scale is None else scale)
        frame_extent = map_item.extent()
        if self.reference_layer is not None and self.reference_layer in layers and any(
                not frame_extent.contains(feature.geometry().asPoint())
                for feature in self.reference_layer.getFeatures()):
            raise ValueError("指定縮尺では基準点が地図枠の外になります。縮尺の分母を大きくしてください")
        if not frame_extent.contains(extent):
            self.append_output_log("指定縮尺では区域の一部が地図枠の外になります。縮尺の分母を大きくしてください。")

    def export_uav(self):
        output = self.output_dir()
        shutil.copytree(ROOT / "html_shinsoku" / "asset", output / "asset", dirs_exist_ok=True)
        ortho = self.olso.currentLayer()
        layers = [self.exclusion_layer, self.application_layer]
        if self.reference_layer is not None:
            layers.insert(0, self.reference_layer)
        if ortho is not None:
            layers.append(ortho)
        layout = self.create_uav_layout(layers)
        exporter = QgsLayoutExporter(layout)
        # HTML内の地図には地理参照情報は不要。レイアウトを直接画像化し、
        # PNGへの地理参照情報追記（GDALの更新非対応）を避ける。
        image = exporter.renderPageToImage(0, dpi=300)
        if image.isNull() or not image.save(str(output / "asset" / "map.png"), "PNG"):
            raise OSError("地図PNGの出力に失敗しました")
        self._editable_projects = EditableProjects(output / "qgz")
        self._editable_projects.capture(layout, "application", "asset/map.png", self.result_layers)
        self.export_shapefile()
        if self.isIchizu.isChecked():
            self.export_location()
        self._editable_projects.save()
        self.write_uav_html()
        if self.makeAssignment.isChecked():
            self.export_assignment_zip()

    def export_assignment_zip(self):
        output = self.output_dir()
        name = self.safe_file_name(self.rinshohan.text())
        image = Path(self.assignmentOlso.filePath())
        archive = output / f"{name} - 提出用.zip"
        image_name = f"{name} - オルソ{image.suffix.lower()}"
        shp_dir = output / "shp"
        shapefile = shp_dir / f"{name} - 申請区域.shp"
        for suffix in (".shp", ".shx", ".dbf", ".prj"):
            if not shapefile.with_suffix(suffix).is_file():
                raise OSError(f"提出用シェープファイルがありません: {shapefile.with_suffix(suffix).name}")
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as bundle:
            for path in sorted(shp_dir.iterdir()):
                if path.is_file() and path.name.startswith(shapefile.stem + "."):
                    bundle.write(path, f"shp/{path.name}")
            bundle.write(image, image_name, compress_type=zipfile.ZIP_STORED)
            # JPEG等の地理参照用ファイルも、画像と同じ新しい名前で同梱する。
            for suffix in (".tfw", ".tifw", ".jgw", ".jpgw", ".pgw", ".pngw", ".wld", ".prj"):
                sidecar = image.with_suffix(suffix)
                if sidecar.is_file():
                    bundle.write(sidecar, str(Path(image_name).with_suffix(suffix)))
            for suffix in (".aux.xml", ".ovr"):
                sidecar = Path(str(image) + suffix)
                if sidecar.is_file():
                    bundle.write(sidecar, image_name + suffix)
        self.append_output_log(f"提出用ZIP: {self.displayed_output_path(archive)}")

    def export_shapefile(self):
        directory = self.output_dir() / "shp"
        directory.mkdir(parents=True, exist_ok=True)
        filename = f"{self.safe_file_name(self.rinshohan.text())} - 申請区域.shp"
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "ESRI Shapefile"
        options.fileEncoding = "UTF-8"
        options.attributes = [self.application_layer.fields().indexFromName(field)
                              for field, _, _, _, _ in ATTRIBUTE_DEFINITIONS]
        options.overrideGeometryType = QgsWkbTypes.MultiPolygon
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
        for field, alias, _, _, _ in ATTRIBUTE_DEFINITIONS:
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
        a3 = self.paper.currentText() == "A3"
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
        # captureで各図面のスタイルを保存済み。同じ地物IDを維持して
        # GeoPackageに申請区域の重複テーブルを作らない。
        # 除去後ポリゴンの境界は除地の青線と重なるため、共有境界を赤線から除く。
        def boundary(geometry):
            geometry = QgsGeometry(geometry)
            geometry.convertToMultiType()
            return QgsGeometry.collectGeometry([
                QgsGeometry.fromPolylineXY(ring)
                for polygon in geometry.asMultiPolygon() for ring in polygon
            ])

        exclusion_boundaries = [boundary(feature.geometry())
                                for feature in self.exclusion_layer.getFeatures()]
        excluded_boundary = QgsGeometry.unaryUnion(exclusion_boundaries) if exclusion_boundaries else None
        location_polygon = QgsVectorLayer(
            f"MultiLineString?crs={self.crs.crs().authid()}", "位置図の区域外周", "memory")
        location_polygon.dataProvider().addAttributes(list(self.application_layer.fields()))
        location_polygon.updateFields()
        for feature in self.application_layer.getFeatures():
            outline = boundary(feature.geometry())
            if excluded_boundary is not None:
                outline = self.checked_operation(outline.difference(excluded_boundary), "位置図の共有境界除去")
            if outline.isEmpty():
                continue
            outline.convertToMultiType()
            line = QgsFeature(location_polygon.fields())
            line.setGeometry(outline)
            line.setAttributes(feature.attributes())
            if not location_polygon.dataProvider().addFeature(line):
                raise ValueError("位置図の区域外周を作成できません")
        location_polygon.setRenderer(QgsSingleSymbolRenderer(QgsLineSymbol.createSimple({
            "line_color": "227,26,28,255", "line_width": "0.53", "line_width_unit": "MM",
            "capstyle": "flat",
        })))
        location_polygon.updateExtents()
        if self.application_layer.labeling():
            location_labels = self.application_layer.labeling().settings()
            location_labels.placement = Qgis.LabelPlacement.Line
            location_polygon.setLabeling(QgsVectorLayerSimpleLabeling(location_labels))
            location_polygon.setLabelsEnabled(self.application_layer.labelsEnabled())
        self.apply_polygon_style(self.exclusion_layer, "location_exclusion_polygon.qml")
        self.exclusion_layer.setLabelsEnabled(False)
        inputs = [self.polygon.currentLayer(), self.jochiPolygon.currentLayer(),
                  self.sagyodoLine.currentLayer(), self.kijunten.currentLayer()]
        canvas = list(self.iface.mapCanvas().layers()) if self.iface else []
        backgrounds = [layer for layer in canvas if layer not in inputs and layer not in self.result_layers]
        ortho = self.olso.currentLayer()
        if ortho and ortho not in backgrounds:
            backgrounds.append(ortho)
        self.set_map_extent(map_item, [self.exclusion_layer, location_polygon] + backgrounds,
                            scale=self.locationScale.scale())
        if not self.set_location_scale_bars(layout, map_item):
            raise ValueError("位置図の縮尺を設定できません")
        path = self.output_dir() / f"{self.safe_file_name(self.rinshohan.text())} - 位置図.pdf"
        settings = QgsLayoutExporter.PdfExportSettings()
        if QgsLayoutExporter(layout).exportToPdf(str(path), settings) != QgsLayoutExporter.Success:
            raise OSError("位置図PDFの出力に失敗しました")
        self._editable_projects.capture(layout, "location", path.name,
                                        self.result_layers + [location_polygon])
        self.apply_polygon_style(self.application_layer, "polygon.qml")
        self.apply_polygon_style(self.exclusion_layer, "exclusion_polygon.qml")

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

    def write_uav_html(self):
        root = ET.parse(str(ROOT / "html_shinsoku" / "index.html"), ET.HTMLParser()).getroot()
        if self.reference_layer is None:
            for legend in root.xpath("//*[@id='map_legend']"):
                legend.getparent().remove(legend)
        for name, text in {
            "seizubi": self.seizubi.date().toString("yyyy年MM月dd日"),
            "seizusha": self.seizusha.text(), "seizujigyosha": self.seizujigyosha.text(),
            "rinshohan": self.rinshohan.text(), "sanrinshoyusha": self.sanrinshoyusha.text(),
            "shinseino": self.shinseibango.text(), "crs": self.format_crs_display(self.crs.crs()),
            "scale": f"1:{self.scale.scale():g}",
        }.items():
            for element in root.xpath(f"//*[@id='{name}']"):
                element.text = text
        for element in root.xpath("//*[@id='scale']"):
            self.apply_latex_parts(element, [
                (f"1:{self.scale.scale():g}", rf"1\mathbin{{:}}{self.scale.scale():g}"),
            ])
        root.find("head/title").text = f"{self.rinshohan.text()} - 申請区域図"
        body = root.find("body")
        body.set("data-paper", self.paper.currentText())
        width, height = (420, 297) if self.paper.currentText() == "A3" else (297, 210)
        factor = self.paper_factor()
        sheet_style = ET.SubElement(root.find("head"), "style")
        sheet_style.text = (f":root{{--paper-factor:{factor};--page-width:{width}mm;--page-height:{height}mm;}}"
                            f"@page{{size:{self.paper.currentText()} landscape;margin:0;}}")
        panel = root.xpath("//*[@id='calc_area']")[0]
        self.replace_children_with_text(panel, "")
        if self.isJochikeisan.isChecked():
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
            is_application = title == "申請面積"
            row = ET.SubElement(panel, "div", {"class": "calc-row calc-application" if is_application else "calc-row"})
            ET.SubElement(row, "div", {"class": "calc-label"}).text = title
            formula = ET.SubElement(row, "div", {"class": "calc-formula"})
            if is_application:
                self.apply_latex_parts(formula, parts[:-2] if self.isJochikeisan.isChecked() else [])
                result = ET.SubElement(row, "div", {"class": "calc-result"})
                self.apply_latex_parts(result, parts[-2:] if self.isJochikeisan.isChecked() else parts)
                ET.SubElement(row, "div", {"class": "calc-result-caption"}).text = "ヘクタール換算"
            elif title == "除地":
                formula.set("class", "calc-formula calc-deduction")
                pending = []
                for part in parts:
                    pending.append(part)
                    if part[0] not in ("+", "=", "≃"):
                        line = ET.SubElement(formula, "div", {"class": "deduction-term"})
                        self.apply_latex_parts(line, pending)
                        pending = []
            else:
                self.apply_latex_parts(formula, parts)
        path = self.output_dir() / "index.html"
        path.write_text("<!DOCTYPE html>\n" + ET.tostring(root, encoding="unicode", method="html"), encoding="utf-8")
        self.append_output_log(f"申請区域図: {self.displayed_output_path(path)}")
