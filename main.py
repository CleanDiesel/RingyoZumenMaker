# -*- coding: utf-8 -*-

from qgis.PyQt.QtWidgets import (
    QWidget, QLabel, QLineEdit, QDoubleSpinBox, QFormLayout,
    QGroupBox, QGridLayout, QHBoxLayout, QVBoxLayout,
    QRadioButton, QButtonGroup, QPushButton, QDockWidget, QMessageBox, QCheckBox,
    QScrollArea, QSizePolicy, QFileDialog, QComboBox
)
from qgis.PyQt.QtCore import QDate, QVariant
from qgis.PyQt.QtGui import QColor, QFont
from qgis.PyQt.QtXml import QDomDocument
from qgis.gui import QgsScaleWidget, QgsMapLayerComboBox, QgsFieldExpressionWidget, QgsFieldComboBox
from qgis.core import (
    Qgis, QgsWkbTypes, QgsMapLayerType, QgsVectorLayerSimpleLabeling, QgsRuleBasedLabeling, QgsMapLayerProxyModel, QgsProcessingModelAlgorithm,
    QgsProject, QgsPrintLayout, QgsLayoutItemMap, QgsLayoutPoint, QgsLayoutSize, QgsUnitTypes, QgsLayoutExporter,
    QgsPalLayerSettings, QgsExpression, QgsExpressionContext, QgsExpressionContextUtils, QgsLayoutItemLabel, QgsLayoutItemPicture,
    QgsLayoutItemScaleBar,
    QgsCoordinateReferenceSystem, QgsVectorFileWriter, QgsReadWriteContext,
    QgsFeature, QgsField, QgsGeometry, QgsVectorLayer,
)
from qgis.PyQt import uic
import os
from lxml import etree as ET
from pathlib import Path
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from datetime import datetime
from numbers import Number
import processing
from html import escape
import math
import re
import shutil
import tempfile
import json
import unicodedata
from .drawing_core import DrawingOperations
from .editable_projects import EditableProjects
from .project_session import ProjectSession
from .unified_project import full_input, metadata, checkpoint_package, source_identity, source_key
from contextlib import contextmanager, ExitStack
import copy
import uuid

FORM_CLASS, _ = uic.loadUiType(os.path.join(os.path.dirname(__file__), "main_dialog.ui"))

OUTPUT_MANIFEST_NAME = ".ringyo_zumen_outputs.json"


class MeasurementBase(DrawingOperations, ProjectSession, QDockWidget, FORM_CLASS):
    def __init__(self, parent=None, iface=None):
        super().__init__(parent)
        self.iface = iface
        self.setObjectName("RingyoZumenMakerDockWidget")

        # A QDockWidget must contain a separate QWidget.  Load the Designer
        # form into that widget while keeping the generated controls available
        # as attributes of this class.
        content = QWidget()
        self.setupUi(content)

        # Keep the progress bar and action buttons visible at the bottom of the
        # dock.  Only the tab area scrolls when vertical space is limited.
        main_layout = content.layout()
        main_layout.removeWidget(self.tabWidget)

        tab_scroll_area = QScrollArea(content)
        tab_scroll_area.setWidgetResizable(True)
        # Ignore the tab contents' preferred height at layout time.  This
        # makes the scroll area shrink first and leaves the controls below it
        # visible even when the dock is short.
        tab_scroll_area.setMinimumHeight(0)
        tab_scroll_area.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Ignored,
        )
        tab_scroll_area.setWidget(self.tabWidget)
        main_layout.insertWidget(0, tab_scroll_area, 1)
        main_layout.setStretch(0, 1)
        main_layout.setStretch(1, 0)
        main_layout.setStretch(2, 0)

        self._content_widget = content
        self._tab_scroll_area = tab_scroll_area
        self.setWidget(content)
        self.setWindowTitle(content.windowTitle())
        self.exit_bt.clicked.connect(self.hide)

        self.xy_table_rows = []
        self.line_xy_table_rows = []
        self.total_line_length = 0.0

        self.submit_bt.clicked.connect(
            lambda: self.on_submit(test=False)
        )

        self.testcalc_bt.clicked.connect(
            lambda: self.on_submit(test=True)
        )

        self.readConfig.clicked.connect(self.load_config_dialog)

        self.setup_shui_toolbox()
        self.setup_haisui_toolbox()
        self.setup_output_options()

        # Dynamic controls above affect the tab's required height.  Preserve
        # that height inside the tab scroll area instead of forcing the whole
        # dock (including its action buttons) off screen.
        self.tabWidget.ensurePolished()
        self.tabWidget.setMinimumHeight(self.tabWidget.sizeHint().height())
        self.tabWidget.updateGeometry()
        tab_scroll_area.updateGeometry()

    def setup_output_options(self):
        self.paper = QComboBox()
        self.paper.addItems(["A4", "A3"])
        self.formLayout_4.addRow("図面の用紙サイズ", self.paper)
        self.locationPaper = QComboBox()
        self.locationPaper.addItems(["A4", "A3"])
        self.formLayout_4.addRow("位置図の用紙サイズ", self.locationPaper)
        self.locationScale = QgsScaleWidget()
        self.locationScale.setScale(5000)
        self.formLayout_4.addRow("位置図の縮尺", self.locationScale)
        self.outputMode = QComboBox()
        self.outputMode.addItems(["作成する（QGZ・GPKG）", "作成しない"])
        self.formLayout_3.addRow("QGZの作成", self.outputMode)
        hint = QLabel("入力設定は実行時に input.config へ自動保存します。")
        hint.setWordWrap(True)
        self.formLayout_3.addRow(hint)
        def location_enabled(enabled):
            for widget in (self.locationPaper, self.locationScale, self.ichizuDirection):
                widget.setEnabled(enabled)
        self.isIchizu.toggled.connect(location_enabled)
        location_enabled(self.isIchizu.isChecked())

    def project_file_name(self):
        return self.safe_file_name(self.rinshohan.text().strip() or "実測図") + ".qgz"

    def paper_factor(self):
        return 420 / 297 if self.paper.currentText() == "A3" else 1.0

    def on_submit(self, test=False):
        from qgis.core import QgsProcessingFeedback
        from .drawing_service import DrawingService, request_from_dock
        from .drawing_inputs import DrawingCancelled
        dock = self

        class Feedback(QgsProcessingFeedback):
            def pushInfo(self, message): dock.append_output_log(message)
            def pushWarning(self, message): dock.append_output_log(message)

        feedback = Feedback()
        feedback.progressChanged.connect(lambda value: dock.progressBar.setValue(round(value)))
        self.open_output_tab()
        self.clear_output_log()
        self.progressBar.setValue(0)
        try:
            if not self.validate_inputs(): return
            if not test and self.outputMode.currentIndex() == 0 and not self.settle_project_edits(): return
            service = DrawingService(request_from_dock(self), feedback, preserve_active_project=True, iface=self.iface)
            if service.report_zip_enabled and not test:
                self._content_widget.setEnabled(False)
                try:
                    self.last_result = service.run()
                finally:
                    self._content_widget.setEnabled(True)
            else:
                self.last_result = service.run(test=test)
            self._generated_scales = service._generated_scales
            self.role_drawings = service.role_drawings
            calculated = {}
            for record in service.shuis + service.haisuis:
                calculated.setdefault(record.registration_uid, []).append(record)
            for page in self.shuis + self.haisuis:
                records = calculated.get(page.drawing_uid, [])
                if len(records) == 1:
                    record = records[0]
                    for field in ('length', 'area', 'pt_layer', 'line_layer', 'area_layer', 'xy_table_rows'):
                        if hasattr(record, field): setattr(page, field, getattr(record, field))
                elif records:
                    page.length = sum(record.length or 0 for record in records)
                    page.area = sum(record.area or 0 for record in records)
                    page.xy_table_rows = [row for record in records for row in record.xy_table_rows]
                    page.pt_layer = page.line_layer = page.area_layer = None
        except DrawingCancelled as error:
            self.append_output_log(str(error))
            self.progressBar.setValue(0)
        except Exception as error:
            QMessageBox.warning(self, "エラー", str(error))
            self.progressBar.setValue(0)










    def open_output_tab(self):
        self.tabWidget.setCurrentWidget(self.tab_5)

    def layer_reference(self, layer):
        if layer is None:
            return None
        return {
            "id": layer.id(),
            "name": layer.name(),
            "source": layer.source(),
            "provider": layer.providerType(),
            "source_key": source_key(layer),
        }

    def resolve_layer_reference(self, reference):
        if not isinstance(reference, dict):
            return None

        project = QgsProject.instance()
        layer_id = self.clean_html_text(reference.get("id"))
        if layer_id:
            layer = project.mapLayer(layer_id)
            if layer is not None:
                return layer

        source = self.clean_html_text(reference.get("source"))
        provider = self.clean_html_text(reference.get("provider"))
        name = self.clean_html_text(reference.get("name"))
        layers = list(project.mapLayers().values())
        if source:
            for layer in layers:
                if layer.source() == source and (not provider or layer.providerType() == provider):
                    return layer
        identity = reference.get('source_key')
        if not identity and provider and (layer_id if provider == 'memory' else source):
            identity = source_identity(provider, source, layer_id)
        packaged_id = (metadata(project) or {}).get('datasets', {}).get(f'input:{identity}') if identity else None
        if packaged_id:
            layer = project.mapLayer(packaged_id)
            if isinstance(layer, QgsVectorLayer) and layer.isValid():
                return layer
        if name:
            for layer in layers:
                if layer.name() == name:
                    return layer
        return None

    def serialize_shui_pages(self):
        pages = []
        for page in self.shuis:
            values = page.values()
            pages.append({
                "name": values.get("name", ""),
                "drawing_uid": page.drawing_uid,
                "deduct_area": bool(values.get("is_jochi")),
                "layer": self.layer_reference(values.get("point_layer")),
                "filter_expression": values.get("filter_exp", ""),
                "point_name_expression": values.get("sokuten_label_exp", ""),
                "point_name_expressions": values.get("sokuten_label_expressions", []),
                "primary_point_name_index": values.get("primary_label_index", 0),
                "sort_expression": values.get("sort_exp", ""),
                "part_expression": values.get("part_exp", ""),
                "object_expression": values.get("object_exp", ""),
            })
        return pages

    def serialize_haisui_pages(self):
        return [self.serialize_haisui_page(page) for page in self.haisuis]

    def serialize_haisui_page(self, page):
        values = page.values()
        return {
            "name": values.get("name", ""),
            "drawing_uid": page.drawing_uid,
            "type": values.get("type", ""),
            "width": values.get("haba", 0.0),
            "has_width": bool(values.get("has_haba", True)),
            "deduct_area": bool(values.get("is_jochi")),
            "layer": self.layer_reference(values.get("point_layer")),
            "filter_expression": values.get("filter_exp", ""),
            "point_name_expression": values.get("sokuten_label_exp", ""),
            "point_name_expressions": values.get("sokuten_label_expressions", []),
            "primary_point_name_index": values.get("primary_label_index", 0),
            "sort_expression": values.get("sort_exp", ""),
        }

    def configuration_data(self):
        return {
            "format": "RingyoZumenMaker.config",
            "version": 3,
            "mode": "measurement",
            "basic": {
                "survey_date": self.sokuryobi.date().toString("yyyy-MM-dd"),
                "surveyor": self.sokuryosha.text(),
                "survey_company": self.sokuryojigyosha.text(),
                "drawing_date": self.seizubi.date().toString("yyyy-MM-dd"),
                "drawing_company": self.seizujigyosha.text(),
                "draftsperson": self.seizusha.text(),
                "forest_compartment": self.rinshohan.text(),
                "forest_owner": self.sanrinshoyusha.text(),
                "application_number": self.application_number.text(),
            },
            "map": {
                "coordinate_decimals": self.ketasu.value(),
                "xy_table_order": self.hyouOrder.currentIndex(),
                "length_display_decimals": self.lengthDecimals.value(),
                "area_display_decimals": self.areaDecimals.value(),
                "hectare_display_decimals": self.haDecimals.value(),
                "scale": self.scale.scale(),
                "paper": self.paper.currentText(),
                "location_paper": self.locationPaper.currentText(),
                "location_scale": self.locationScale.scale(),
                "crs": self.crs.crs().authid(),
                "show_deduction": self.isJochikeisan.isChecked(),
                "minimum_exclusion_area_a": self.minEx.value(),
                "create_location_map": self.isIchizu.isChecked(),
                "location_direction": self.ichizuDirection.currentIndex(),
            },
            "output": {
                "directory": self.fileName.filePath(),
                "backup_qgz": self.backupQgz.isChecked(),
                "create_qgz": self.outputMode.currentIndex() == 0,
            },
            "polygons": {
                "enabled": self.isShui.isChecked(),
                "items": self.serialize_shui_pages(),
            },
            "lines": {
                "enabled": self.isHaisui.isChecked(),
                "items": self.serialize_haisui_pages(),
            },
        }


    def register_generated_output_path(self, path):
        final_output_dir = Path(self.fileName.filePath()).resolve()
        try:
            relative_path = Path(path).resolve().relative_to(final_output_dir)
        except (OSError, ValueError):
            return

        normalized = self.normalized_generated_output_path(relative_path.as_posix())
        if normalized is None:
            return
        manifest_path = final_output_dir / OUTPUT_MANIFEST_NAME
        generated_paths = self.previous_generated_output_paths(final_output_dir)
        generated_paths.add(normalized)
        self.write_output_manifest(manifest_path, generated_paths)

    def load_config_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "設定ファイルを読み込む",
            "",
            "設定ファイル (*.config);;すべてのファイル (*)",
        )
        if not path:
            return
        self.load_config_file(path)

    def load_config_file(self, path):
        path = Path(path)
        try:
            with open(path, "r", encoding="utf-8") as config_file:
                data = json.load(config_file)
        except (OSError, UnicodeError, json.JSONDecodeError) as e:
            QMessageBox.warning(self, "エラー", f"設定ファイルを読み込めません:\n{path}\n{e}")
            return False

        if not isinstance(data, dict) or data.get("format") != "RingyoZumenMaker.config":
            QMessageBox.warning(self, "エラー", f"RingyoZumenMakerの設定ファイルではありません:\n{path}")
            return False
        try:
            missing_layers = self.apply_configuration(data)
        except (AttributeError, TypeError, ValueError) as e:
            QMessageBox.warning(self, "エラー", f"設定ファイルの内容を反映できません:\n{path}\n{e}")
            return False

        self.append_output_log(f"設定ファイルを読み込みました: {path}")
        if missing_layers:
            QMessageBox.warning(
                self,
                "設定読み込み",
                "次のポイントレイヤは現在のプロジェクトで見つからないため、空欄にしました:\n"
                + "\n".join(missing_layers),
            )
        return True

    def apply_configuration(self, data):
        if data.get("mode") not in (None, "measurement") or data.get("version", 1) not in (1, 2, 3):
            raise ValueError("この実測図プラグインに対応しない設定形式です")
        basic = data.get("basic") if isinstance(data.get("basic"), dict) else {}
        map_settings = data.get("map") if isinstance(data.get("map"), dict) else {}
        output = data.get("output") if isinstance(data.get("output"), dict) else {}
        polygons = data.get("polygons")
        if not isinstance(polygons, dict):
            polygons = data.get("perimeters")
        if not isinstance(polygons, dict):
            polygons = {}

        lines = data.get("lines")
        if not isinstance(lines, dict):
            lines = data.get("drainage")
        if not isinstance(lines, dict):
            lines = {}

        text_values = {
            "sokuryosha": basic.get("surveyor", ""),
            "sokuryojigyosha": basic.get("survey_company", ""),
            "seizujigyosha": basic.get("drawing_company", ""),
            "seizusha": basic.get("draftsperson", ""),
            "rinshohan": basic.get("forest_compartment", ""),
            "sanrinshoyusha": basic.get("forest_owner", ""),
            "application_number": basic.get("application_number", ""),
        }
        for widget_name, value in text_values.items():
            getattr(self, widget_name).setText(self.clean_html_text(value))

        self.set_date_from_config(self.sokuryobi, basic.get("survey_date", ""))
        self.set_date_from_config(self.seizubi, basic.get("drawing_date", ""))

        decimals = map_settings.get("coordinate_decimals", self.ketasu.minimum())
        self.ketasu.setValue(int(decimals or 0))
        try:
            xy_table_order = int(map_settings.get("xy_table_order", 0) or 0)
        except (TypeError, ValueError):
            xy_table_order = 0
        self.hyouOrder.setCurrentIndex(
            xy_table_order if xy_table_order in (0, 1, 2) else 0
        )
        self.lengthDecimals.setValue(int(map_settings.get("length_display_decimals", 1)))
        self.areaDecimals.setValue(int(map_settings.get("area_display_decimals", 0)))
        self.haDecimals.setValue(int(map_settings.get("hectare_display_decimals", 2)))
        scale = map_settings.get("scale")
        if scale not in (None, ""):
            self.scale.setScale(float(scale))
        crs_text = self.clean_html_text(map_settings.get("crs"))
        if crs_text:
            crs = QgsCoordinateReferenceSystem(crs_text)
            if crs.isValid():
                self.crs.setCrs(crs)

        self.isJochikeisan.setChecked(bool(map_settings.get("show_deduction", False)))
        minimum_exclusion_area = map_settings.get("minimum_exclusion_area_a")
        if minimum_exclusion_area not in (None, ""):
            self.minEx.setValue(float(minimum_exclusion_area))
        self.isIchizu.setChecked(bool(map_settings.get("create_location_map", False)))
        try:
            location_direction = int(map_settings.get("location_direction", 0) or 0)
        except (TypeError, ValueError):
            location_direction = 0
        self.ichizuDirection.setCurrentIndex(
            location_direction if location_direction in (0, 1) else 0
        )
        self.fileName.setFilePath(self.clean_html_text(output.get("directory")))
        self.backupQgz.setChecked(bool(output.get("backup_qgz", False)))
        self.paper.setCurrentText(map_settings.get("paper", "A4"))
        self.locationPaper.setCurrentText(map_settings.get("location_paper", "A4"))
        self.locationScale.setScale(float(map_settings.get("location_scale", map_settings.get("scale", 5000))))
        self.outputMode.setCurrentIndex(0 if output.get("create_qgz", True) else 1)

        missing_layers = []
        missing_layers.extend(self.restore_shui_pages_from_config(polygons.get("items", [])))
        missing_layers.extend(self.restore_haisui_pages_from_config(
            lines.get("items", []),
            default_type=map_settings.get("drainage_type", "排水"),
        ))
        self.isShui.setChecked(bool(polygons.get("enabled", False)))
        self.isHaisui.setChecked(bool(lines.get("enabled", False)))
        return missing_layers

    def set_date_from_config(self, widget, value):
        date = QDate.fromString(self.clean_html_text(value), "yyyy-MM-dd")
        if date.isValid():
            widget.setDate(date)

    def clear_shui_pages(self):
        while self.shuiToolBox.count() > 0:
            widget = self.shuiToolBox.widget(0)
            self.shuiToolBox.removeItem(0)
            if widget is not None:
                widget.deleteLater()
        self.shuis = []
        self.shui_count = 0

    def restore_shui_pages_from_config(self, items):
        if not isinstance(items, list):
            raise ValueError("ポリゴンの設定形式が正しくありません")
        self.clear_shui_pages()
        missing_layers = []
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("ポリゴンの設定形式が正しくありません")
            self.add_shui_page()
            page = self.shuis[-1]
            page.drawing_uid = item.get("drawing_uid") or uuid.uuid4().hex
            page.name_edit.setText(self.clean_html_text(item.get("name")))
            page.is_jochi.setChecked(bool(item.get("deduct_area", False)))
            reference = item.get("layer")
            layer = self.resolve_layer_reference(reference)
            page.point_layer.setLayer(layer)
            page.filter_exp.setExpression(self.clean_html_text(item.get("filter_expression")))
            page.sokuten_labels.set_expressions(
                item.get("point_name_expressions"),
                item.get("primary_point_name_index", 0),
                fallback_expression=self.clean_html_text(item.get("point_name_expression")),
            )
            page.sort_exp.setExpression(self.clean_html_text(item.get("sort_expression")))
            if isinstance(reference, dict) and layer is None:
                missing_layers.append(f"ポリゴン {page.index}: {reference.get('name', '')}")
        return missing_layers

    def clear_haisui_pages(self):
        while self.toolBox.count() > 0:
            widget = self.toolBox.widget(0)
            self.toolBox.removeItem(0)
            if widget is not None:
                widget.deleteLater()
        self.haisuis = []
        self.haisui_count = 0

    def restore_haisui_pages_from_config(
        self,
        items,
        default_type="排水",
    ):
        if not isinstance(items, list):
            raise ValueError("ラインの設定形式が正しくありません")
        self.clear_haisui_pages()
        missing_layers = []
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("ラインの設定形式が正しくありません")
            self.add_haisui_page()
            page = self.haisuis[-1]
            page.drawing_uid = item.get("drawing_uid") or uuid.uuid4().hex
            page.name_edit.setText(self.clean_html_text(item.get("name")))
            page.type_edit.setText(self.clean_html_text(item.get("type", default_type)))
            page.haba_spin.setValue(float(item.get("width") or 0.0))
            # 旧設定のshow_widthは表示専用だったため、幅の有無には流用しない。
            has_width = bool(item.get("has_width", True))
            page.isHaba.setChecked(has_width)
            page.is_jochi.setChecked(has_width and bool(item.get("deduct_area", False)))
            reference = item.get("layer")
            layer = self.resolve_layer_reference(reference)
            page.point_layer.setLayer(layer)
            page.filter_exp.setExpression(self.clean_html_text(item.get("filter_expression")))
            page.sokuten_labels.set_expressions(
                item.get("point_name_expressions"),
                item.get("primary_point_name_index", 0),
                fallback_expression=self.clean_html_text(item.get("point_name_expression")),
            )
            page.sort_exp.setExpression(self.clean_html_text(item.get("sort_expression")))
            if isinstance(reference, dict) and layer is None:
                missing_layers.append(f"ライン {page.index}: {reference.get('name', '')}")
        return missing_layers

    def get_shui_values(self):
        return [page.values() for page in self.shuis]

    def get_haisui_values(self):
        return [page.values() for page in self.haisuis]




    def setup_shui_toolbox(self):
        while self.shuiToolBox.count() > 0:
            widget = self.shuiToolBox.widget(0)
            self.shuiToolBox.removeItem(0)
            if widget is not None:
                widget.deleteLater()

        self.shuis = []
        self.shui_count = 0

        try:
            self.shui_add.clicked.disconnect(self.add_shui_page)
        except TypeError:
            pass
        self.shui_add.clicked.connect(self.add_shui_page)

    def add_shui_page(self):
        self.shui_count += 1
        page = ShuiPage(self.shui_count, self)
        index = self.shuiToolBox.addItem(page, str(len(self.shuis) + 1))
        self.shuiToolBox.setCurrentIndex(index)
        page.delete_button.clicked.connect(lambda: self.remove_shui_page(page))
        self.shuis.append(page)
        self.renumber_shui_pages()

    def remove_shui_page(self, page):
        index = self.shuiToolBox.indexOf(page)
        if index != -1:
            self.shuiToolBox.removeItem(index)
        if page in self.shuis:
            self.shuis.remove(page)
        page.deleteLater()
        self.renumber_shui_pages()
        if not self.shuis:
            self.shui_count = 0

    def renumber_shui_pages(self):
        for i, page in enumerate(self.shuis, start=1):
            page.index = i
            page.setObjectName(f"shui_page_{i}")
            toolbox_index = self.shuiToolBox.indexOf(page)
            if toolbox_index != -1:
                self.shuiToolBox.setItemText(toolbox_index, str(i))

    def setup_haisui_toolbox(self):
        while self.toolBox.count() > 0:
            widget = self.toolBox.widget(0)
            self.toolBox.removeItem(0)
            if widget is not None:
                widget.deleteLater()

        self.haisuis = []
        self.haisui_count = 0

        try:
            self.haisui_add.clicked.disconnect(self.add_haisui_page)
        except TypeError:
            pass
        self.haisui_add.clicked.connect(self.add_haisui_page)

    def add_haisui_page(self):
        self.haisui_count += 1
        page = HaisuiPage(self.haisui_count, self)

        index = self.toolBox.addItem(page, str(len(self.haisuis) + 1))
        self.toolBox.setCurrentIndex(index)

        page.delete_button.clicked.connect(lambda: self.remove_haisui_page(page))

        self.haisuis.append(page)
        self.renumber_haisui_pages()


    def remove_haisui_page(self, page):
        index = self.toolBox.indexOf(page)

        if index != -1:
            self.toolBox.removeItem(index)

        if page in self.haisuis:
            self.haisuis.remove(page)

        page.deleteLater()
        self.renumber_haisui_pages()
        if not self.haisuis:
            self.haisui_count = 0

    def renumber_haisui_pages(self):
        for i, page in enumerate(self.haisuis, start=1):
            page.index = i
            page.setObjectName(f"haisui_page_{i}")
            toolbox_index = self.toolBox.indexOf(page)
            if toolbox_index != -1:
                self.toolBox.setItemText(toolbox_index, str(i))
    


    def append_output_log(self, message):
        if hasattr(self, "outputlog"):
            if not hasattr(self, "output_log_lines"):
                self.output_log_lines = []
            self.output_log_lines.append(str(message))
            self.outputlog.setPlainText("\n".join(self.output_log_lines))
            scrollbar = self.outputlog.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())

    def clear_output_log(self):
        self.output_log_lines = []
        if hasattr(self, "outputlog"):
            self.outputlog.clear()























    def haisui_area_term(self, item):
        name = self.clean_html_text(item["name"])
        calculation = f"{self.format_length(item['length'])}×{self.format_width(item['haba'])}"
        return f"{name} {calculation}" if name else calculation





    def xy_table_row_sort_key(self, row):
        if isinstance(row, dict):
            return self.measurement_name_sort_key(row.get("sort_value"))
        try:
            row_element = ET.fromstring(row)
            name_cell = row_element.find("td")
            name = "" if name_cell is None else "".join(name_cell.itertext())
        except (ET.XMLSyntaxError, TypeError, ValueError):
            name = row
        return self.measurement_name_sort_key(name)

    def measurement_name_sort_key(self, value):
        if value is None:
            return (0,)
        if isinstance(value, bool):
            return (1, int(value))
        if isinstance(value, (Number, Decimal)) and not isinstance(value, complex):
            number = float(value)
            if math.isnan(number):
                return (2, 1, 0.0)
            return (2, 0, number)

        text = unicodedata.normalize(
            "NFKC",
            self.clean_html_text(value),
        ).casefold()
        natural_parts = tuple(
            (1, int(part), len(part)) if part.isdecimal() else (0, part)
            for part in re.split(r"(\d+)", text)
        )
        return (3, natural_parts)













    

    














    def safe_gpkg_layer_name(self, value):
        text = self.clean_html_text(value).strip()
        for char in '\\/:*?"<>|':
            text = text.replace(char, "_")
        text = "_".join(text.split())
        return text or "layer"








class MeasurementLabelExpressionsWidget(QWidget):
    def __init__(self, layer_combo, parent=None):
        super().__init__(parent)
        self.layer_combo = layer_combo
        self.rows = []
        self.primary_group = QButtonGroup(self)
        self.primary_group.setExclusive(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.rows_layout = QVBoxLayout()
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(4)
        layout.addLayout(self.rows_layout)

        self.add_button = QPushButton("測点名を追加")
        self.add_button.clicked.connect(lambda checked=False: self.add_expression())
        layout.addWidget(self.add_button)

        self.layer_combo.layerChanged.connect(self.set_layer)
        self.add_expression(primary=True)

    def add_expression(self, expression="", primary=False):
        row_widget = QWidget(self)
        row_layout = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(4)

        primary_button = QRadioButton("主")
        expression_widget = QgsFieldExpressionWidget()
        expression_widget.setLayer(self.layer_combo.currentLayer())
        expression_widget.setExpression(self._clean_expression(expression))
        delete_button = QPushButton("削除")

        row_layout.addWidget(primary_button)
        row_layout.addWidget(expression_widget, 1)
        row_layout.addWidget(delete_button)
        self.rows_layout.addWidget(row_widget)

        row = {
            "widget": row_widget,
            "primary": primary_button,
            "expression": expression_widget,
            "delete": delete_button,
        }
        self.rows.append(row)
        self.primary_group.addButton(primary_button)
        delete_button.clicked.connect(lambda checked=False, current=row: self.remove_expression(current))

        if primary or len(self.rows) == 1:
            primary_button.setChecked(True)
        self.update_delete_buttons()
        return row

    def remove_expression(self, row):
        if row not in self.rows:
            return
        was_primary = row["primary"].isChecked()
        self.primary_group.removeButton(row["primary"])
        self.rows.remove(row)
        self.rows_layout.removeWidget(row["widget"])
        row["widget"].deleteLater()
        if not self.rows:
            self.add_expression(primary=True)
            return
        if was_primary:
            self.rows[0]["primary"].setChecked(True)
        self.update_delete_buttons()

    def update_delete_buttons(self):
        enabled = len(self.rows) > 1
        for row in self.rows:
            row["delete"].setEnabled(enabled)

    def set_layer(self, layer):
        for row in self.rows:
            row["expression"].setLayer(layer)

    def expressions(self):
        return [row["expression"].expression().strip() for row in self.rows]

    def primary_index(self):
        for index, row in enumerate(self.rows):
            if row["primary"].isChecked():
                return index
        return 0

    def primary_expression(self):
        expressions = self.expressions()
        return expressions[self.primary_index()] if expressions else ""

    def set_expressions(self, expressions, primary_index=0, fallback_expression=""):
        if not isinstance(expressions, list):
            expressions = []
        expressions = [self._clean_expression(value) for value in expressions]
        if not expressions:
            expressions = [self._clean_expression(fallback_expression)]

        for row in list(self.rows):
            self.primary_group.removeButton(row["primary"])
            self.rows.remove(row)
            self.rows_layout.removeWidget(row["widget"])
            row["widget"].deleteLater()

        try:
            primary_index = int(primary_index)
        except (TypeError, ValueError):
            primary_index = 0
        if primary_index < 0 or primary_index >= len(expressions):
            primary_index = 0

        for index, expression in enumerate(expressions):
            self.add_expression(expression, primary=index == primary_index)
        self.update_delete_buttons()

    @staticmethod
    def _clean_expression(value):
        return "" if value is None else str(value).replace("\r\n", "\n").replace("\r", "\n")


class ShuiPage(QWidget):
    def __init__(self, index, parent=None):
        super().__init__(parent)
        self.index = index
        self.drawing_uid = uuid.uuid4().hex
        self.setObjectName(f"shui_page_{index}")

        layout = QFormLayout(self)
        self.length = None
        self.area = None
        self.xy_table_rows = []
        self.pt_layer = None
        self.line_layer = None
        self.area_layer = None

        self.name_edit = QLineEdit()
        self.name_edit.setText(chr(64 + index) if index <= 26 else str(index))
        layout.addRow("名前", self.name_edit)

        self.is_jochi = QCheckBox("除地として扱う")
        layout.addRow("除地", self.is_jochi)

        point_group = QGroupBox("ポリゴンポイント指定")
        point_layout = QGridLayout(point_group)
        self.point_layer = QgsMapLayerComboBox()
        self.point_layer.setFilters(QgsMapLayerProxyModel.PointLayer)
        self.filter_exp = QgsFieldExpressionWidget()
        self.filter_exp.setLayer(self.point_layer.currentLayer())
        self.point_layer.layerChanged.connect(self.filter_exp.setLayer)
        point_layout.addWidget(QLabel("ポイントレイヤ"), 0, 0)
        point_layout.addWidget(self.point_layer, 0, 1)
        point_layout.addWidget(QLabel("式（空欄ならレイヤ全体）"), 1, 0)
        point_layout.addWidget(self.filter_exp, 1, 1)
        layout.addRow(point_group)

        attr_group = QGroupBox("使用する属性")
        attr_layout = QGridLayout(attr_group)
        self.sokuten_labels = MeasurementLabelExpressionsWidget(self.point_layer)
        self.sort_exp = QgsFieldExpressionWidget()
        self.sort_exp.setLayer(self.point_layer.currentLayer())
        self.point_layer.layerChanged.connect(self.sort_exp.setLayer)
        attr_layout.addWidget(QLabel("測点名指定"), 0, 0)
        attr_layout.addWidget(self.sokuten_labels, 0, 1)
        attr_layout.addWidget(QLabel("結合順指定"), 1, 0)
        attr_layout.addWidget(self.sort_exp, 1, 1)
        self.part_exp = QgsFieldExpressionWidget()
        self.part_exp.setLayer(self.point_layer.currentLayer())
        self.point_layer.layerChanged.connect(self.part_exp.setLayer)
        self.part_exp.setToolTip('離れた輪郭ごとに異なる値を指定します。空欄なら一つの輪郭。名前・面積・周長は地物単位でまとめます。')
        attr_layout.addWidget(QLabel("パートの区分（任意）"), 3, 0)
        attr_layout.addWidget(self.part_exp, 3, 1)
        self.object_exp = QgsFieldExpressionWidget()
        self.object_exp.setLayer(self.point_layer.currentLayer())
        self.point_layer.layerChanged.connect(self.object_exp.setLayer)
        self.object_exp.setToolTip('同じ値の測点を一つのポリゴンとして扱います。値が違えば別ポリゴン。空欄ならこの登録全体で一つです。')
        attr_layout.addWidget(QLabel("地物の区分（任意）"), 2, 0)
        attr_layout.addWidget(self.object_exp, 2, 1)
        layout.addRow(attr_group)

        self.delete_button = QPushButton("削除")
        layout.addRow(self.delete_button)

    def values(self):
        label_expressions = self.sokuten_labels.expressions()
        return {
            "name": self.name_edit.text(),
            "is_jochi": self.is_jochi.isChecked(),
            "point_layer": self.point_layer.currentLayer(),
            "filter_exp": self.filter_exp.expression(),
            "sokuten_label_exp": self.sokuten_labels.primary_expression(),
            "sokuten_label_expressions": label_expressions,
            "primary_label_index": self.sokuten_labels.primary_index(),
            "sort_exp": self.sort_exp.expression(),
            "part_exp": self.part_exp.expression(),
            "object_exp": self.object_exp.expression(),
        }


class HaisuiPage(QWidget):
    def __init__(self, index, parent=None, required_width=False):
        super().__init__(parent)

        self.required_width = required_width
        self.index = index
        self.drawing_uid = uuid.uuid4().hex

        self.setObjectName(f"haisui_page_{index}")

        layout = QFormLayout(self)
    
        self.length = None
        self.xy_table_rows = []
        self.pt_layer = None
        self.line_layer = None

        # 名前
        self.name_edit = QLineEdit()
        self.name_edit.setText(chr(64 + index))  # 1=A, 2=B
        layout.addRow("名前", self.name_edit)

        self.type_edit = QLineEdit()
        self.type_edit.setText("排水")
        layout.addRow("種別", self.type_edit)

        self.isHaba = None
        if not required_width:
            self.isHaba = QCheckBox("あり")
            self.isHaba.setChecked(True)
            layout.addRow("幅", self.isHaba)

        self.haba_spin = QDoubleSpinBox()
        self.haba_spin.setValue(1.0)
        self.haba_spin.setDecimals(2)
        self.haba_spin.setSuffix(" m")
        layout.addRow("幅（m）", self.haba_spin)

        self.is_jochi = QCheckBox("除地として扱う")
        layout.addRow("除地", self.is_jochi)
        if self.isHaba is not None:
            self.isHaba.toggled.connect(self.update_width_state)
        self.update_width_state(required_width or self.isHaba.isChecked())

        # ポイント指定
        point_group = QGroupBox("ポイント指定")
        point_layout = QGridLayout(point_group)

        self.point_layer = QgsMapLayerComboBox()
        self.point_layer.setFilters(QgsMapLayerProxyModel.PointLayer)

        self.filter_exp = QgsFieldExpressionWidget()
        self.filter_exp.setLayer(self.point_layer.currentLayer())

        self.point_layer.layerChanged.connect(self.filter_exp.setLayer)

        point_layout.addWidget(QLabel("ポイントレイヤ"), 0, 0)
        point_layout.addWidget(self.point_layer, 0, 1)
        point_layout.addWidget(QLabel("式（空欄ならレイヤ全体）"), 1, 0)
        point_layout.addWidget(self.filter_exp, 1, 1)

        layout.addRow(point_group)

        # 使用する属性
        attr_group = QGroupBox("使用する属性")
        attr_layout = QGridLayout(attr_group)

        self.sokuten_labels = MeasurementLabelExpressionsWidget(self.point_layer)
        self.sort_exp = QgsFieldExpressionWidget()

        self.sort_exp.setLayer(self.point_layer.currentLayer())

        self.point_layer.layerChanged.connect(self.sort_exp.setLayer)

        attr_layout.addWidget(QLabel("測点名指定"), 0, 0)
        attr_layout.addWidget(self.sokuten_labels, 0, 1)
        attr_layout.addWidget(QLabel("結合順指定"), 1, 0)
        attr_layout.addWidget(self.sort_exp, 1, 1)

        layout.addRow(attr_group)

        # 削除ボタン
        self.delete_button = QPushButton("削除")
        layout.addRow(self.delete_button)

    def values(self):
        label_expressions = self.sokuten_labels.expressions()
        has_width = self.required_width or self.isHaba.isChecked()
        return {
            "name": self.name_edit.text(),
            "type": self.type_edit.text(),
            "haba": self.haba_spin.value(),
            "has_haba": has_width,
            "is_jochi": has_width and self.is_jochi.isChecked(),
            "point_layer": self.point_layer.currentLayer(),
            "filter_exp": self.filter_exp.expression(),
            "sokuten_label_exp": self.sokuten_labels.primary_expression(),
            "sokuten_label_expressions": label_expressions,
            "primary_label_index": self.sokuten_labels.primary_index(),
            "sort_exp": self.sort_exp.expression(),
        }

    def update_width_state(self, has_width):
        has_width = self.required_width or has_width
        self.haba_spin.setEnabled(has_width)
        self.is_jochi.setEnabled(has_width)
        if not has_width:
            self.is_jochi.setChecked(False)


from .measurement_workflow import MeasurementWorkflow


class Main(MeasurementWorkflow, MeasurementBase):
    """Survey drawings with land, exclusions and ancillary works input roles."""
    pass
