"""Designer widget bindings and per-feature data-defined application attributes."""
import math
from decimal import Decimal
from pathlib import Path

from qgis.PyQt.QtCore import QDate, QDateTime
from qgis.PyQt.QtWidgets import QComboBox, QDateEdit, QLineEdit, QAbstractSpinBox
from qgis.core import (
    Qgis, QgsCoordinateReferenceSystem, QgsExpression, QgsExpressionContext,
    QgsExpressionContextGenerator, QgsExpressionContextUtils, QgsMapLayerProxyModel,
    QgsProperty, QgsPropertyDefinition, QgsVariantUtils,
)


CONFIG_VERSION = 6

ATTRIBUTE_INPUTS = (
    ("shinkokyoku", "振興局"), ("shichoson", "市町村"), ("rinpan", "林班"),
    ("shohan", "小班"), ("edaban", "枝番"), ("jigyoCode", "事業"),
    ("seizujigyosha", "製図事業者"), ("shinseibango", "申請No"),
    ("seizubi2", "製図日"), ("seizusha2", "製図者"), ("sanrinshoyusha2", "所有者"),
)
OVERRIDE_FIELDS = tuple(field for field, _ in ATTRIBUTE_INPUTS)
LAYER_INPUTS = {
    "polygon": (QgsMapLayerProxyModel.PolygonLayer, ("polygonFilter", "polygonName", "edaban")),
    "jochiPolygon": (QgsMapLayerProxyModel.PolygonLayer, ("jochiFilter", "jochiName")),
    "sagyodoLine": (QgsMapLayerProxyModel.LineLayer, ("sagyodoFilter", "jochiNameSagyodo")),
    "singleLine": (QgsMapLayerProxyModel.LineLayer,
                   ("singleLineFilter", "singleLineName", "singleLineEdaban")),
    "kijunten": (QgsMapLayerProxyModel.PointLayer, ("kijuntenExp",)),
    "olso": (QgsMapLayerProxyModel.RasterLayer, ()),
}


def prefixed(prefix, name):
    return prefix + name[0].upper() + name[1:] if prefix else name


class LayerExpressionContext(QgsExpressionContextGenerator):
    def __init__(self, combo):
        super().__init__()
        self.combo = combo

    def createExpressionContext(self):
        context = QgsExpressionContext()
        layer = self.combo.currentLayer()
        context.appendScopes(QgsExpressionContextUtils.globalProjectLayerScopes(layer))
        if layer is not None:
            context.setFields(layer.fields())
        return context


class UavInputs:
    def setup_uav_inputs(self):
        self._override_bindings = {}
        self._expression_contexts = []
        for combo_name, (layer_filter, expressions) in LAYER_INPUTS.items():
            combo = getattr(self, combo_name)
            combo.setFilters(layer_filter)
            combo.setAllowEmptyLayer(True)
            combo.setLayer(None)
            for name in expressions:
                widget = getattr(self, name)
                widget.setExpression("")
                self.bind_expression_layer(widget, None)
                combo.layerChanged.connect(
                    lambda layer, field=widget: self.bind_expression_layer(field, layer))
                widget.setToolTip("空欄のフィルタは全地物。式は選択したレイヤの地物ごとに評価します。"
                                  if "Filter" in name or name == "kijuntenExp" else
                                  "選択したレイヤの地物ごとに評価します。空欄ならラベルなし。")
        for prefix, combo_name in (("", "polygon"),
                                   ("singleLine", "singleLine")):
            for field in OVERRIDE_FIELDS:
                self.setup_override(prefixed(prefix, field), combo_name)
            for field in ("rinpan", "shohan"):
                widget = getattr(self, prefixed(prefix, field))
                widget.setMaximum(2147483647)
                widget.setMinimum(-1)
                widget.setSpecialValueText(" ")
                widget.setValue(-1)
            date = getattr(self, prefixed(prefix, "seizubi2"))
            date.setDisplayFormat("yyyy年MM月dd日")
            date.setDate(QDate.currentDate())
        for field, combo_name in (("hukuin", "sagyodoLine"), ("singleLineWidth", "singleLine")):
            widget = getattr(self, field)
            widget.setMaximum(100000)
            widget.setDecimals(2)
            self.setup_override(field, combo_name)
            combo = getattr(self, combo_name)
            combo.layerChanged.connect(lambda *_, n=field: self.update_override_enabled(n))
            self.update_override_enabled(field)
        self.minKijuntenkan.setMaximum(100000)
        self.minKijuntenkan.setDecimals(3)
        self.minKijuntenkan.setValue(20)
        self.minKijuntenkan.setEnabled(self.kijunten.currentLayer() is not None)
        self.kijunten.layerChanged.connect(lambda layer: self.minKijuntenkan.setEnabled(layer is not None))
        self.scale.setScale(5000)
        self.locationScale.setScale(5000)
        self.isIchizu.toggled.connect(self.update_location_inputs)
        self.update_location_inputs()
        self.assignmentOlso.setFilter("オルソ画像 (*.tif *.tiff *.jpg *.jpeg *.png *.jp2 *.ecw *.img)")
        self.assignmentOlso.setToolTip("空欄ならコピーしません。指定画像と地理参照ファイルを図面ごとにコピーします。")
        self.tabWidget.setCurrentIndex(0)
        self.result_layers = []
        self.drawings = []

    @staticmethod
    def bind_expression_layer(widget, layer):
        expression = widget.expression()
        widget.setLayer(layer)
        widget.setExpression(expression)

    def update_location_inputs(self, *_):
        for name in ("locationPaper", "locationScale", "ichizuDirection"):
            getattr(self, name).setEnabled(self.isIchizu.isChecked())

    def setup_override(self, field, combo_name):
        button = getattr(self, field + "Override")
        numeric = field in ("rinpan", "shohan",
                            "singleLineRinpan", "singleLineShohan", "hukuin", "singleLineWidth")
        kind = QgsPropertyDefinition.StandardPropertyTemplate.Double if numeric else QgsPropertyDefinition.StandardPropertyTemplate.String
        definition = QgsPropertyDefinition(field, field, kind)
        combo = getattr(self, combo_name)
        button.init(0, QgsProperty(), definition, combo.currentLayer(), False)
        generator = LayerExpressionContext(combo)
        self._expression_contexts.append(generator)
        button.registerExpressionContextGenerator(generator)
        button.setUsageInfo("左の入力値を、選択レイヤの地物ごとの属性または式で上書きします。")
        self._override_bindings[field] = combo_name
        if field.startswith("singleLine") and field != "singleLineWidth":
            button.setUsageInfo("施行地の値を使う場合は「施行地の値を継承」を選びます。フィールド・式で個別に上書きすることもできます。")
            button.menu().aboutToShow.connect(lambda n=field: self.add_inheritance_action(n))
        combo.layerChanged.connect(button.setVectorLayer)
        combo.layerChanged.connect(lambda *_, n=field: self.update_override_enabled(n))
        button.changed.connect(lambda n=field: self.update_override_enabled(n))
        button.activated.connect(lambda *_, n=field: self.update_override_enabled(n))
        self.update_override_enabled(field)

    @staticmethod
    def inheritance_expression(field):
        return "@ringyo_application_" + field

    def add_inheritance_action(self, name):
        button = getattr(self, name + "Override")
        field = name[len("singleLine"):]
        field = field[0].lower() + field[1:]
        menu = button.menu()
        action = menu.addAction("施行地の値を継承")
        action.setCheckable(True)
        action.setChecked(button.isActive() and button.toProperty().asExpression() == self.inheritance_expression(field))
        action.triggered.connect(lambda checked, n=name, f=field: self.set_attribute_inheritance(n, f, checked))
        menu.insertAction(menu.actions()[0], action)

    def set_attribute_inheritance(self, name, field, enabled):
        prop = QgsProperty.fromExpression(self.inheritance_expression(field), enabled)
        getattr(self, name + "Override").setToProperty(prop)
        self.update_override_enabled(name)

    def application_attribute_value(self, field):
        layer = self.polygon.currentLayer()
        features = self.selected_features("polygon", "polygonFilter") if layer else []
        if features:
            return self.override_value(field, layer, features[0])
        widget = getattr(self, field)
        if hasattr(widget, "expression"):
            expression = QgsExpression(widget.expression())
            context = QgsExpressionContext()
            context.appendScopes(QgsExpressionContextUtils.globalProjectLayerScopes(layer))
            value = expression.evaluate(context)
            return "" if QgsVariantUtils.isNull(value) else value
        return self.input_value(widget)

    def update_override_enabled(self, field):
        combo = getattr(self, self._override_bindings[field])
        button = getattr(self, field + "Override")
        available = combo.currentLayer() is not None
        button.setEnabled(available or field in OVERRIDE_FIELDS or
                          (field.startswith("singleLine") and field != "singleLineWidth"))
        if button.isActive() and button.toProperty().asExpression().startswith("@ringyo_application_"):
            button.setToolTip("施行地の値を継承しています。ボタンを開いて継承を解除すると、個別入力に戻ります。")
        getattr(self, field).setEnabled(not button.isActive() and
                                       (available or field not in ("hukuin", "singleLineWidth")))

    @staticmethod
    def input_value(widget):
        if isinstance(widget, QDateEdit):
            return widget.date()
        if isinstance(widget, QComboBox):
            return widget.currentText()
        if isinstance(widget, QAbstractSpinBox):
            value = widget.value()
            return None if widget.minimum() == -1 and value == -1 else value
        if hasattr(widget, "expression"):
            return widget.expression()
        return widget.text()

    def evaluate_expression(self, text, layer, feature, description, default=""):
        if not text.strip():
            return default
        expression = QgsExpression(text)
        context = QgsExpressionContext()
        context.appendScopes(QgsExpressionContextUtils.globalProjectLayerScopes(layer))
        context.setFeature(feature)
        context.setFields(layer.fields())
        if expression.hasParserError() or not expression.prepare(context):
            raise ValueError(f"{layer.name()} / {description}の式: "
                             f"{expression.parserErrorString() or expression.evalErrorString()}")
        result = expression.evaluate(context)
        if expression.hasEvalError():
            raise ValueError(f"{layer.name()} 地物ID={feature.id()} / {description}: {expression.evalErrorString()}")
        return default if result is None or QgsVariantUtils.isNull(result) else result

    def evaluate_attribute(self, widget_name, layer, feature, default=""):
        return self.evaluate_expression(getattr(self, widget_name).expression(), layer, feature, widget_name, default)

    def override_value(self, name, layer, feature):
        button = getattr(self, name + "Override")
        if button.isActive():
            if name.startswith("singleLine"):
                field = name[len("singleLine"):]
                field = field[0].lower() + field[1:]
                if button.toProperty().asExpression() == self.inheritance_expression(field):
                    return self.application_attribute_value(field)
            # Do not silently use the literal input when an active expression fails.
            return self.evaluate_expression(button.toProperty().asExpression(), layer, feature, name, None)
        widget = getattr(self, name)
        if hasattr(widget, "expression"):
            return self.evaluate_attribute(name, layer, feature)
        return self.input_value(widget)

    def selected_features(self, combo_name, filter_name):
        layer = getattr(self, combo_name).currentLayer()
        if layer is None:
            return []
        text = getattr(self, filter_name).expression().strip()
        if not text:
            return list(layer.getFeatures())
        return [feature for feature in layer.getFeatures()
                if self.evaluate_expression(f"CASE WHEN ({text}) THEN TRUE ELSE FALSE END",
                                            layer, feature, filter_name, False)]

    def numeric_override(self, name, layer, feature, positive=True):
        value = self.override_value(name, layer, feature)
        try:
            number = float(value)
            if not math.isfinite(number) or (number <= 0 if positive else number < 0):
                raise ValueError()
        except (TypeError, ValueError, OverflowError):
            raise ValueError(f"{layer.name()} 地物ID={feature.id()}: {name}は"
                             f"{'0より大きい' if positive else '0以上の'}有限の数値で指定してください")
        if name in ("hukuin", "singleLineWidth"):
            number = round(number, 2)
            if number <= 0:
                raise ValueError(f"{layer.name()} 地物ID={feature.id()}: 幅は0.01m以上で指定してください")
        return number

    def polygon_attributes(self, layer, feature, name, prefix=""):
        values = {}
        for field, alias in ATTRIBUTE_INPUTS:
            widget_name = prefixed(prefix, field)
            value = self.override_value(widget_name, layer, feature)
            if value is None or (isinstance(value, str) and not value.strip()):
                value = None if alias in ("林班", "小班") else ""
            if alias in ("林班", "小班"):
                if value is None:
                    values[alias] = None
                    continue
                try:
                    number = Decimal(str(value))
                    if not number.is_finite() or number != number.to_integral_value() or not 0 <= number <= 2147483647:
                        raise ValueError()
                    value = int(number)
                except Exception:
                    raise ValueError(f"{layer.name()} 地物ID={feature.id()}: {alias}は0以上の整数で指定してください")
            elif alias == "製図日":
                if isinstance(value, QDateTime):
                    value = value.date()
                elif not isinstance(value, QDate):
                    value = QDate.fromString(str(value), "yyyy-MM-dd")
                if not value.isValid():
                    raise ValueError(f"{layer.name()} 地物ID={feature.id()}: 製図年月日は日付で指定してください")
            else:
                value = self.clean_html_text(value)
            values[alias] = value
        values.update({"label": name, "source_id": feature.id()})
        return values

    def validate_inputs(self):
        self._selected_inputs = {}
        for name, (_, expressions) in LAYER_INPUTS.items():
            layer = getattr(self, name).currentLayer()
            if layer is not None and not layer.isValid():
                raise ValueError(f"{name}: 有効なレイヤを指定してください")
            if name in ("polygon", "singleLine"):
                self._selected_inputs[name] = self.selected_features(name, expressions[0])
        if not any(self._selected_inputs[name] for name in ("polygon", "singleLine")):
            raise ValueError("施行地または付帯作工物で、少なくとも1つの対象地物を指定してください")
        crs = self.crs.crs()
        if not crs.isValid() or crs.isGeographic() or crs.mapUnits() != Qgis.DistanceUnit.Meters:
            raise ValueError("メートル単位の平面直角座標系などを指定してください")
        for name in ("scale", "locationScale") if self.isIchizu.isChecked() else ("scale",):
            if not math.isfinite(getattr(self, name).scale()) or getattr(self, name).scale() <= 0:
                raise ValueError("有効な縮尺を指定してください")
        if not self.fileName.filePath() or not Path(self.fileName.filePath()).is_dir():
            raise ValueError("出力先には存在するフォルダを指定してください")
        if self.isSaveConfig.currentIndex() == 2 and not self.saveConfig.filePath().strip():
            raise ValueError("設定保存ファイルを指定してください")
        image = self.assignmentOlso.filePath().strip()
        if image and (not Path(image).is_file() or Path(image).suffix.lower() not in
                      (".tif", ".tiff", ".jpg", ".jpeg", ".png", ".jp2", ".ecw", ".img")):
            raise ValueError("コピーするオルソ画像には存在する対応形式のファイルを指定してください")
        return True

    def configuration_data(self):
        literals = {}
        for prefix in ("", "singleLine"):
            for field, _ in ATTRIBUTE_INPUTS:
                if field == "edaban":
                    continue
                name = prefixed(prefix, field)
                value = self.input_value(getattr(self, name))
                literals[name] = value.toString("yyyy-MM-dd") if isinstance(value, QDate) else value
        return {
            "format": "RingyoZumenMaker.config", "version": CONFIG_VERSION, "mode": "uav",
            "map": {"crs": self.crs.crs().authid(), "scale": self.scale.scale(),
                    "paper": self.paper.currentText(), "location_paper": self.locationPaper.currentText(),
                    "show_deduction": self.isJochikeisan.isChecked(),
                    "area_display_decimals": self.areaDecimals.value(), "hectare_display_decimals": self.haDecimals.value(),
                    "create_location_map": self.isIchizu.isChecked(), "location_direction": self.ichizuDirection.currentIndex(),
                    "location_scale": self.locationScale.scale(), "minimum_exclusion_area_a": self.minJochi.value(),
                    "road_width_m": self.hukuin.value(), "single_line_width_m": self.singleLineWidth.value(),
                    "minimum_reference_distance_m": self.minKijuntenkan.value()},
            "layers": {name: self.layer_reference(getattr(self, name).currentLayer()) for name in LAYER_INPUTS},
            "expressions": {name: getattr(self, name).expression()
                            for _, expressions in LAYER_INPUTS.values() for name in expressions},
            "attributes": literals,
            "overrides": {name: getattr(self, name + "Override").toProperty().toVariant() for name in self._override_bindings},
            "output": {"directory": self.fileName.filePath(), "backup_generated": self.backupQgz.isChecked(),
                       "create_qgz": self.outputMode.currentIndex() == 0,
                       "config_save_mode": self.isSaveConfig.currentIndex(), "config_file": self.saveConfig.filePath(),
                       "assignment_ortho": self.assignmentOlso.filePath()},
        }

    def apply_configuration(self, data):
        if (not isinstance(data, dict) or data.get("format") != "RingyoZumenMaker.config"
                or data.get("version") != CONFIG_VERSION or data.get("mode") != "uav"):
            raise ValueError("現在の形式の設定ファイルを指定してください")
        settings = data.get("map", {})
        self.crs.setCrs(QgsCoordinateReferenceSystem(settings.get("crs", "")))
        self.scale.setScale(float(settings.get("scale") or 5000))
        self.locationScale.setScale(float(settings.get("location_scale", 5000)))
        self.paper.setCurrentIndex(1 if settings.get("paper") == "A3" else 0)
        self.locationPaper.setCurrentIndex(1 if settings.get("location_paper", "A4") == "A3" else 0)
        for name, key in (("isJochikeisan", "show_deduction"), ("isIchizu", "create_location_map")):
            getattr(self, name).setChecked(bool(settings.get(key, False)))
        self.ichizuDirection.setCurrentIndex(1 if settings.get("location_direction") == 1 else 0)
        for name, key, default in (("areaDecimals", "area_display_decimals", 0), ("haDecimals", "hectare_display_decimals", 2),
                                    ("minJochi", "minimum_exclusion_area_a", 1), ("hukuin", "road_width_m", 0),
                                    ("singleLineWidth", "single_line_width_m", 0), ("minKijuntenkan", "minimum_reference_distance_m", 20)):
            getattr(self, name).setValue(float(settings.get(key, default)) if name not in ("areaDecimals", "haDecimals")
                                         else int(settings.get(key, default)))
        missing = []
        for name in LAYER_INPUTS:
            reference = data.get("layers", {}).get(name)
            layer = self.resolve_layer_reference(reference)
            getattr(self, name).setLayer(layer)
            if reference and layer is None:
                missing.append(reference.get("name", name))
        expressions = data.get("expressions", {})
        for _, names in LAYER_INPUTS.values():
            for name in names:
                getattr(self, name).setExpression(self.clean_html_text(expressions.get(name, "")))
        literals = data.get("attributes", {})
        for prefix in ("", "singleLine"):
            for field, _ in ATTRIBUTE_INPUTS:
                if field == "edaban":
                    continue
                name = prefixed(prefix, field)
                widget = getattr(self, name)
                value = literals.get(name)
                if isinstance(widget, QDateEdit):
                    self.set_date_from_config(widget, value)
                elif isinstance(widget, QComboBox):
                    widget.setCurrentIndex(max(0, widget.findText(str(value or ""))))
                elif isinstance(widget, QAbstractSpinBox):
                    widget.setValue(int(value) if value is not None else -1)
                else:
                    widget.setText(self.clean_html_text(value))
        for name in self._override_bindings:
            prop = QgsProperty()
            variant = data.get("overrides", {}).get(name)
            if variant is not None:
                if not prop.loadVariant(variant):
                    raise ValueError(f"{name}の上書き設定を読み込めません")
            getattr(self, name + "Override").setToProperty(prop)
            self.update_override_enabled(name)
        output = data.get("output", {})
        self.outputMode.setCurrentIndex(0 if output.get("create_qgz", True) else 1)
        self.fileName.setFilePath(self.clean_html_text(output.get("directory")))
        self.backupQgz.setChecked(bool(output.get("backup_generated", False)))
        self.assignmentOlso.setFilePath(self.clean_html_text(output.get("assignment_ortho")))
        mode = int(output.get("config_save_mode", 0))
        self.isSaveConfig.setCurrentIndex(mode if mode in (0, 1, 2) else 0)
        self.saveConfig.setFilePath(self.clean_html_text(output.get("config_file")))
        self.update_save_config_enabled()
        return missing
