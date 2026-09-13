# -*- coding: utf-8 -*-

from qgis.PyQt.QtWidgets import (
    QWidget, QLabel, QLineEdit, QDoubleSpinBox, QFormLayout,
    QGroupBox, QGridLayout, QHBoxLayout, QVBoxLayout,
    QRadioButton, QButtonGroup, QPushButton, QDockWidget, QMessageBox, QCheckBox,
    QScrollArea, QSizePolicy, QFileDialog
)
from qgis.PyQt.QtCore import QDate, QVariant
from qgis.PyQt.QtGui import QColor, QFont
from qgis.PyQt.QtXml import QDomDocument
from qgis.gui import QgsMapLayerComboBox, QgsFieldExpressionWidget, QgsFieldComboBox
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
from decimal import Decimal
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
from .editable_projects import EditableProjects

FORM_CLASS, _ = uic.loadUiType(os.path.join(os.path.dirname(__file__), "main_dialog.ui"))


class Main(QDockWidget, FORM_CLASS):
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

        self.submit_bt.clicked.connect(
            lambda: self.on_submit(test=False)
        )

        self.testcalc_bt.clicked.connect(
            lambda: self.on_submit(test=True)
        )

        self.readConfig.clicked.connect(self.load_config_dialog)
        self.saveConfig.setFilter("設定ファイル (*.config)")
        self.isSaveConfig.currentIndexChanged.connect(self.update_save_config_enabled)

        self.setup_shui_toolbox()
        self.setup_haisui_toolbox()
        self.update_save_config_enabled()

        # Dynamic controls above affect the tab's required height.  Preserve
        # that height inside the tab scroll area instead of forcing the whole
        # dock (including its action buttons) off screen.
        self.tabWidget.ensurePolished()
        self.tabWidget.setMinimumHeight(self.tabWidget.sizeHint().height())
        self.tabWidget.updateGeometry()
        tab_scroll_area.updateGeometry()

    def on_submit(self, test=False):
        self.open_output_tab()
        self.clear_output_log()

        if not self.validate_inputs():
            return

        self.progressBar.setValue(0)

        final_output_dir = Path(self.fileName.filePath())
        staging_dir = None

        try:
            if not test:
                staging_dir = Path(tempfile.mkdtemp(
                    prefix=".ringyo_zumen_tmp_",
                    dir=final_output_dir,
                ))
                self._output_dir_override = staging_dir
                self._final_output_dir = final_output_dir

            self.LayerSet = {}
            self.xy_table_rows = []
            self.progressBar.setValue(20)

            self.htmlValues = {}
            self.progressBar.setValue(40)

            if not self.map_make(test=test):
                self.progressBar.setValue(0)
                return

            if test:
                if not self.make_html(write_file=False):
                    self.progressBar.setValue(0)
                    return
                self.progressBar.setValue(100)
                return

            self.progressBar.setValue(60)

            if not self.copy_assets():
                self.progressBar.setValue(0)
                return
            self.progressBar.setValue(80)

            if not self.make_html():
                self.progressBar.setValue(0)
                return

            if not self.commit_staged_output(
                staging_dir,
                final_output_dir,
                backup_qgz=self.backupQgz.isChecked(),
            ):
                self.progressBar.setValue(0)
                return

            if not self.save_config_file():
                self.progressBar.setValue(0)
                return

            self.append_output_log(f"出力を確定しました: {final_output_dir}")
            self.progressBar.setValue(100)
        except Exception as e:
            QMessageBox.warning(
                self,
                "エラー",
                f"出力処理に失敗しました:\n{e}"
            )
            self.progressBar.setValue(0)
        finally:
            if hasattr(self, "_output_dir_override"):
                del self._output_dir_override
            if hasattr(self, "_final_output_dir"):
                del self._final_output_dir
            if staging_dir is not None and staging_dir.exists():
                shutil.rmtree(staging_dir, ignore_errors=True)

    def output_dir(self):
        if hasattr(self, "_output_dir_override"):
            return Path(self._output_dir_override)
        return Path(self.fileName.filePath())

    def displayed_output_path(self, path):
        path = Path(path)
        if not hasattr(self, "_final_output_dir"):
            return path

        try:
            relative_path = path.relative_to(self.output_dir())
        except ValueError:
            return path
        return Path(self._final_output_dir) / relative_path

    def commit_staged_output(self, staging_dir, final_output_dir, backup_qgz=False):
        staging_dir = Path(staging_dir)
        final_output_dir = Path(final_output_dir)
        backup_dir = staging_dir / ".backup"
        current_qgz_dir = final_output_dir / "qgz"
        qgz_backup_dir = None
        staged_files = [
            path
            for path in staging_dir.rglob("*")
            if path.is_file() and backup_dir not in path.parents
        ]
        committed = []

        try:
            if backup_qgz and current_qgz_dir.is_dir():
                created_at = datetime.fromtimestamp(
                    current_qgz_dir.stat().st_ctime
                ).strftime("%Y%m%d-%H%M%S")
                qgz_backup_dir = final_output_dir / f"qgz-{created_at}"
                suffix = 2
                while qgz_backup_dir.exists():
                    qgz_backup_dir = final_output_dir / f"qgz-{created_at}-{suffix}"
                    suffix += 1
                os.replace(current_qgz_dir, qgz_backup_dir)

            for source_path in staged_files:
                relative_path = source_path.relative_to(staging_dir)
                target_path = final_output_dir / relative_path
                target_path.parent.mkdir(parents=True, exist_ok=True)

                backup_path = None
                if target_path.exists():
                    backup_path = backup_dir / relative_path
                    backup_path.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(target_path, backup_path)

                record = {
                    "target": target_path,
                    "backup": backup_path,
                    "installed": False,
                }
                committed.append(record)
                os.replace(source_path, target_path)
                record["installed"] = True
        except OSError as e:
            for record in reversed(committed):
                target_path = record["target"]
                backup_path = record["backup"]
                try:
                    if record["installed"] and target_path.exists():
                        target_path.unlink()
                    if backup_path is not None and backup_path.exists():
                        target_path.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(backup_path, target_path)
                except OSError:
                    pass

            if qgz_backup_dir is not None and qgz_backup_dir.exists():
                try:
                    if current_qgz_dir.exists():
                        resolved_qgz = current_qgz_dir.resolve()
                        if (
                            resolved_qgz.parent != final_output_dir.resolve()
                            or resolved_qgz.name != "qgz"
                        ):
                            raise OSError(f"復旧対象外のフォルダです: {resolved_qgz}")
                        shutil.rmtree(resolved_qgz)
                    os.replace(qgz_backup_dir, current_qgz_dir)
                except OSError:
                    pass

            QMessageBox.warning(
                self,
                "エラー",
                f"完成したファイルを出力先へ反映できません:\n{e}"
            )
            return False

        return True

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
                "deduct_area": bool(values.get("is_jochi")),
                "layer": self.layer_reference(values.get("point_layer")),
                "filter_expression": values.get("filter_exp", ""),
                "point_name_expression": values.get("sokuten_label_exp", ""),
                "point_name_expressions": values.get("sokuten_label_expressions", []),
                "primary_point_name_index": values.get("primary_label_index", 0),
                "sort_expression": values.get("sort_exp", ""),
            })
        return pages

    def serialize_haisui_pages(self):
        pages = []
        for page in self.haisuis:
            values = page.values()
            pages.append({
                "name": values.get("name", ""),
                "type": values.get("type", ""),
                "width": values.get("haba", 0.0),
                "show_width": bool(values.get("show_haba", True)),
                "deduct_area": bool(values.get("is_jochi")),
                "layer": self.layer_reference(values.get("point_layer")),
                "filter_expression": values.get("filter_exp", ""),
                "point_name_expression": values.get("sokuten_label_exp", ""),
                "point_name_expressions": values.get("sokuten_label_expressions", []),
                "primary_point_name_index": values.get("primary_label_index", 0),
                "sort_expression": values.get("sort_exp", ""),
            })
        return pages

    def configuration_data(self):
        return {
            "format": "RingyoZumenMaker.config",
            "version": 2,
            "basic": {
                "survey_date": self.sokuryobi.date().toString("yyyy-MM-dd"),
                "surveyor": self.sokuryosha.text(),
                "survey_company": self.sokuryojigyosha.text(),
                "drawing_date": self.seizubi.date().toString("yyyy-MM-dd"),
                "drawing_company": self.seizujigyosha.text(),
                "draftsperson": self.seizusha.text(),
                "forest_compartment": self.rinshohan.text(),
                "forest_owner": self.sanrinshoyusha.text(),
            },
            "map": {
                "coordinate_decimals": self.ketasu.value(),
                "xy_table_order": self.hyouOrder.currentIndex(),
                "scale": self.scale.scale(),
                "crs": self.crs.crs().authid(),
                "show_deduction": self.isJochikeisan.isChecked(),
                "minimum_exclusion_area_a": self.minEx.value(),
                "create_location_map": self.isIchizu.isChecked(),
            },
            "output": {
                "directory": self.fileName.filePath(),
                "backup_qgz": self.backupQgz.isChecked(),
                "config_save_mode": self.isSaveConfig.currentIndex(),
                "config_file": self.saveConfig.filePath(),
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

    def update_save_config_enabled(self, *_):
        self.saveConfig.setEnabled(self.isSaveConfig.currentIndex() == 2)

    def selected_config_output_path(self):
        mode = self.isSaveConfig.currentIndex()
        if mode == 0:
            return None
        if mode == 1:
            return Path(self.fileName.filePath()) / "input.config"

        raw_path = self.saveConfig.filePath().strip()
        if not raw_path:
            raise ValueError("設定保存ファイルを指定してください")
        path = Path(raw_path)
        if path.suffix.lower() != ".config":
            path = Path(f"{path}.config")
            self.saveConfig.setFilePath(str(path))
        return path

    def save_config_file(self):
        try:
            path = self.selected_config_output_path()
        except ValueError as e:
            QMessageBox.warning(self, "エラー", str(e))
            return False
        if path is None:
            return True
        if not path.parent.exists() or not path.parent.is_dir():
            QMessageBox.warning(self, "エラー", f"設定ファイルの保存先フォルダがありません:\n{path.parent}")
            return False

        temp_path = None
        try:
            file_descriptor, temp_name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
            )
            temp_path = Path(temp_name)
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as config_file:
                json.dump(self.configuration_data(), config_file, ensure_ascii=False, indent=2)
                config_file.write("\n")
            os.replace(temp_path, path)
        except (OSError, TypeError, ValueError) as e:
            if temp_path is not None and temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass
            QMessageBox.warning(self, "エラー", f"設定ファイルを保存できません:\n{path}\n{e}")
            return False

        self.append_output_log(f"設定ファイルを書き込みました: {path}")
        return True

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
        self.fileName.setFilePath(self.clean_html_text(output.get("directory")))
        self.backupQgz.setChecked(bool(output.get("backup_qgz", False)))
        self.saveConfig.setFilePath(self.clean_html_text(output.get("config_file")))
        try:
            save_mode = int(output.get("config_save_mode", 0) or 0)
        except (TypeError, ValueError):
            save_mode = 0
        self.isSaveConfig.setCurrentIndex(save_mode if save_mode in (0, 1, 2) else 0)
        self.update_save_config_enabled()

        missing_layers = []
        missing_layers.extend(self.restore_shui_pages_from_config(polygons.get("items", [])))
        missing_layers.extend(self.restore_haisui_pages_from_config(
            lines.get("items", []),
            default_type=map_settings.get("drainage_type", "排水"),
            default_show_width=map_settings.get("show_drainage_width", True),
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
        default_show_width=True,
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
            page.name_edit.setText(self.clean_html_text(item.get("name")))
            page.type_edit.setText(self.clean_html_text(item.get("type", default_type)))
            page.haba_spin.setValue(float(item.get("width") or 0.0))
            page.isHaba.setChecked(bool(item.get("show_width", default_show_width)))
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
                missing_layers.append(f"ライン {page.index}: {reference.get('name', '')}")
        return missing_layers

    def get_shui_values(self):
        return [page.values() for page in self.shuis]

    def get_haisui_values(self):
        return [page.values() for page in self.haisuis]

    def apply_layer_color(self, layer, color, include_labels=False):
        if layer is None or color is None:
            return

        renderer = layer.renderer()
        symbol = renderer.symbol() if renderer is not None else None
        if symbol is not None:
            symbol.setColor(color)

        if include_labels:
            labeling = layer.labeling()
            if labeling is not None:
                settings = labeling.settings()
                text_format = settings.format()
                text_format.setColor(color)
                settings.setFormat(text_format)

                current_callout = settings.callout()
                callout = current_callout.clone() if current_callout is not None else None
                line_symbol = (
                    callout.lineSymbol()
                    if callout is not None and hasattr(callout, "lineSymbol")
                    else None
                )
                if line_symbol is not None:
                    line_symbol.setColor(color)
                    settings.setCallout(callout)

                layer.setLabeling(QgsVectorLayerSimpleLabeling(settings))

        layer.triggerRepaint()

    def configure_mix_line_label(self, layer, name, color=None):
        layer.loadNamedStyle(
            os.path.join(os.path.dirname(__file__), "styles", "mix_haisui_line.qml")
        )
        label_text = self.clean_html_text(name).strip()
        if label_text:
            labeling = layer.labeling()
            settings = labeling.settings() if labeling else QgsPalLayerSettings()
            escaped_text = label_text.replace("'", "''")
            settings.fieldName = f"'{escaped_text}'"
            settings.isExpression = True
            text_format = settings.format()
            text_format.setFont(QFont("Yu Gothic"))
            text_format.setSize(9)
            text_format.setSizeUnit(Qgis.RenderUnit.Points)
            settings.setFormat(text_format)
            layer.setLabeling(QgsVectorLayerSimpleLabeling(settings))
            layer.setLabelsEnabled(True)
        else:
            # ラベルを無効化すれば、同じラベル設定に属する引き出し線も描画されない。
            layer.setLabelsEnabled(False)
        self.apply_layer_color(layer, color, include_labels=True)

    def create_mix_perimeter_label_layer(self, pages):
        """Place perimeter names near their polygons without leader lines."""
        named_pages = [
            page for page in pages
            if page.line_layer is not None
            and self.clean_html_text(page.values().get("name")).strip()
        ]
        if not named_pages:
            return None

        crs = named_pages[0].line_layer.crs()
        label_layer = QgsVectorLayer(
            f"MultiPolygon?crs={crs.authid()}",
            "ポリゴン名（配置用）",
            "memory",
        )
        provider = label_layer.dataProvider()
        provider.addAttributes([QgsField("label", QVariant.String)])
        label_layer.updateFields()

        for page in named_pages:
            name = self.clean_html_text(page.values().get("name")).strip()
            for source_feature in page.line_layer.getFeatures():
                geometry = source_feature.geometry()
                parts = geometry.asMultiPolyline() if geometry.isMultipart() else [geometry.asPolyline()]
                closed_parts = [part for part in parts if len(part) >= 4 and part[0] == part[-1]]
                if not closed_parts:
                    continue

                feature = QgsFeature(label_layer.fields())
                feature.setAttribute("label", name)
                feature.setGeometry(QgsGeometry.fromMultiPolygonXY([[part] for part in closed_parts]))
                provider.addFeature(feature)

        label_layer.updateExtents()
        if label_layer.featureCount() == 0:
            return None

        symbol = label_layer.renderer().symbol() if label_layer.renderer() else None
        if symbol is not None:
            symbol.setOpacity(0)

        # Place all names together so adjacent polygons share collision detection.
        template_layer = named_pages[0].line_layer
        labeling = template_layer.labeling()
        settings = labeling.settings() if labeling else QgsPalLayerSettings()
        settings.fieldName = "label"
        settings.isExpression = False
        settings.geometryGeneratorEnabled = False
        settings.geometryGenerator = ""
        settings.geometryGeneratorType = QgsWkbTypes.PolygonGeometry
        settings.placement = Qgis.LabelPlacement.OutsidePolygons
        settings.dist = 1.5
        if settings.callout() is not None:
            settings.callout().setEnabled(False)
        settings.obstacle = True
        settings.obstacleFactor = 2.0
        settings.obstacleType = QgsPalLayerSettings.ObstacleType.PolygonBoundary
        placement_settings = settings.placementSettings()
        placement_settings.setOverlapHandling(
            Qgis.LabelOverlapHandling.PreventOverlap
        )
        placement_settings.setAllowDegradedPlacement(True)
        settings.setPlacementSettings(placement_settings)
        text_format = settings.format()
        text_format.setFont(QFont("Yu Gothic"))
        text_format.setSize(9)
        text_format.setSizeUnit(Qgis.RenderUnit.Points)
        settings.setFormat(text_format)
        label_layer.setLabeling(QgsVectorLayerSimpleLabeling(settings))
        label_layer.setLabelsEnabled(True)
        self.apply_layer_color(label_layer, QColor("black"), include_labels=True)
        return label_layer

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
    
    def make_html(self, write_file=True):
        parser = ET.HTMLParser()
        html_path = os.path.join(os.path.dirname(__file__), 'html_shinsoku', 'index.html')
        try:
            root = ET.parse(html_path, parser)
        except (OSError, ET.XMLSyntaxError) as e:
            QMessageBox.warning(
                self,
                "エラー",
                f"HTMLテンプレートを読み込めません:\n{e}"
            )
            return False

        self.htmlValues.setdefault("shui_length", 0)
        self.htmlValues.setdefault("area", 0)
        self.htmlValues.setdefault("area2", 0)
        self.htmlValues.setdefault("area_ha", "0.00")
        
        self.htmlValues.update({
            "sokuryobi": self.sokuryobi.text(),
            "sokuryosha": self.sokuryosha.text(),
            "sokuryojigyosha": self.sokuryojigyosha.text(),
            "scale": f"1/{int(self.scale.scale())}",
            "sanrinshoyusha": self.sanrinshoyusha.text(),
            "rinshohan": self.rinshohan.text(),
            "crs": self.format_crs_display(self.crs.crs()),
            "seizubi": self.seizubi.text(),
            "seizusha": self.seizusha.text() or self.sokuryosha.text(),
            "seizujigyosha": self.seizujigyosha.text() or self.sokuryojigyosha.text(),
        })

        for key, value in self.htmlValues.items():
            elems = root.xpath(f"//*[@id='{key}']")
            for elem in elems:
                elem.text = self.clean_html_text(value)

        title = root.xpath("//title")[0]
        title.text = f"{self.htmlValues['rinshohan']} - 実測図"

        if self.isShui.isChecked():
            for page in self.shuis:
                values = page.values()
                option_panel = f"shui_{page.index}"
                name_text = self.clean_html_text(values.get("name")).strip()
                dropdown_name = name_text or str(page.index)
                self.update_zumen_options(root, option_panel, f"ポリゴン {dropdown_name}")
                self.update_xy_tables(root, page.xy_table_rows, option_panel)
                self.add_main_map(root, option_panel)
                self.add_shui_detail(root, option_panel, page.length, page.area)
                if name_text:
                    self.add_map_name(root, option_panel, name_text, "shui")
        if self.isHaisui.isChecked():
            for page in self.haisuis:
                values = page.values()
                haisui_type = self.clean_html_text(values.get("type")).strip()
                name_text = self.clean_html_text(values.get("name")).strip()
                named_display = f"{haisui_type} {name_text}".strip()
                dropdown_name = named_display or f"{haisui_type or 'ライン'} {page.index}"
                self.update_zumen_options(root, f"haisui_{page.index}", dropdown_name)
                self.update_xy_tables(root, page.xy_table_rows, f"haisui_{page.index}")
                self.add_main_map(root, f"haisui_{page.index}")
                self.add_haisui_length(root, f"haisui_{page.index}", page.length)
                displayed_haba = values.get("haba") if values.get("show_haba") else None
                self.add_haisui_detail(
                    root,
                    f"haisui_{page.index}",
                    page.length,
                    displayed_haba,
                )
                if named_display:
                    self.add_map_name(
                        root, f"haisui_{page.index}", named_display, "haisui"
                    )
        if self.isShui.isChecked() or self.isHaisui.isChecked():
            self.update_zumen_options(root, "mix", "全体")
            self.update_xy_tables(root, self.xy_table_rows, "mix")
            self.add_main_map(root, "mix")

        self.add_calc_data(
            root,
            write_to_html=(
                (self.isShui.isChecked() or self.isHaisui.isChecked())
                and self.isJochikeisan.isChecked()
            ),
        )
            
        output_dir = self.output_dir()
        output_path = str(output_dir / "index.html")
        self.clean_html_tree(root)
        if not write_file:
            self.append_output_log("試算のためHTMLは書き込みません")
            return True

        displayed_path = self.displayed_output_path(output_path)
        self.append_output_log(f"HTMLを書き込みます: {displayed_path}")
        try:
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(ET.tostring(root, pretty_print=True, encoding="unicode", method="html"))
        except OSError as e:
            QMessageBox.warning(
                self,
                "エラー",
                f"HTMLを書き込めません:\n{displayed_path}\n{e}"
            )
            return False

        return True

    def clean_html_text(self, value):
        if value is None:
            return ""

        return str(value).replace("\r\n", "\n").replace("\r", "\n")

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

    def add_calc_data(self, root, write_to_html=True):
        calcs = root.xpath("//*[@id='calc']")
        if not calcs:
            return

        calc = calcs[0]
        polygon_items = []
        if self.isShui.isChecked():
            for page in self.shuis:
                if page.area is None:
                    continue
                values = page.values()
                polygon_items.append({
                    "name": self.clean_html_text(values.get("name")).strip(),
                    "area": float(page.area),
                    "is_jochi": bool(values.get("is_jochi")),
                })

        line_items = []
        if self.isHaisui.isChecked():
            for page in self.haisuis:
                if page.length is None:
                    continue
                values = page.values()
                line_items.append({
                    "name": self.clean_html_text(values.get("name")).strip(),
                    "type": self.clean_html_text(values.get("type")).strip() or "ライン",
                    "length": float(page.length),
                    "haba": float(values.get("haba") or 0.0),
                    "is_jochi": bool(values.get("is_jochi")),
                })

        minimum_exclusion_area = self.minimum_exclusion_area_m2()
        requested_jochi_lines = [item for item in line_items if item["is_jochi"]]
        jochi_lines = [
            item for item in requested_jochi_lines
            if self.haisui_raw_area(item) >= minimum_exclusion_area
        ]
        jochi_polygons = [item for item in polygon_items if item["is_jochi"]]
        work_polygons = [item for item in polygon_items if not item["is_jochi"]]

        jochi_line_length = sum(item["length"] for item in jochi_lines)
        work_area = sum(item["area"] for item in work_polygons)
        exclusion_area = (
            sum(item["area"] for item in jochi_polygons)
            + sum(self.haisui_calc_area(item) for item in jochi_lines)
        )
        result_area = work_area - exclusion_area

        has_below_minimum_area = minimum_exclusion_area > 0 and any(
            self.haisui_raw_area(item) < minimum_exclusion_area
            for item in requested_jochi_lines
        )
        minimum_exclusion_area_text = self.format_area_int(minimum_exclusion_area)

        line_groups = {}
        for item in line_items:
            line_groups.setdefault(item["type"], []).append(item)

        left_rows = []
        for line_type, typed_items in line_groups.items():
            total = sum(item["length"] for item in typed_items)
            parts = self.latex_sum_parts(
                [self.latex_length_term(item) for item in typed_items],
                self.latex_quantity(total, r"\mathrm{m}", self.format_length),
            )
            left_rows.append((f"{line_type}：", parts))
            self.append_output_log(
                f"{line_type}延長: {' + '.join(self.haisui_length_term(item) for item in typed_items)}"
                f" = {self.format_length(total)}m"
            )

        if jochi_lines:
            jochi_length_parts = self.latex_sum_parts(
                [self.latex_length_term(item) for item in jochi_lines],
                self.latex_quantity(
                    jochi_line_length,
                    r"\mathrm{m}",
                    self.format_length,
                ),
            )
            left_rows.append(("除地ライン：", jochi_length_parts))
            self.append_output_log(
                "除地ライン延長: "
                + " + ".join(self.haisui_length_term(item) for item in jochi_lines)
                + f" = {self.format_length(jochi_line_length)}m"
            )

        right_rows = []
        if work_polygons:
            work_area_parts = self.latex_sum_parts(
                [
                    self.latex_named_quantity(
                        item["name"], item["area"], r"\mathrm{m}^{2}"
                    )
                    for item in work_polygons
                ],
                self.latex_quantity(
                    work_area, r"\mathrm{m}^{2}", self.format_area_int
                ),
            )
            right_rows.append(("施行地面積：", work_area_parts, ""))
            self.append_output_log(
                f"施行地面積: {self.format_area_int(work_area)}m²"
            )

        exclusion_terms = [
            self.latex_named_quantity(
                item["name"], item["area"], r"\mathrm{m}^{2}"
            )
            for item in jochi_polygons
        ] + [self.latex_area_term(item) for item in jochi_lines]
        if exclusion_terms:
            exclusion_area_parts = self.latex_sum_parts(
                exclusion_terms,
                self.latex_quantity(
                    exclusion_area,
                    r"\mathrm{m}^{2}",
                    self.format_area_decimal,
                ),
            )
            exclusion_area_parts.extend([
                ("≃", r"\simeq"),
                self.latex_quantity(
                    exclusion_area,
                    r"\mathrm{m}^{2}",
                    self.format_area_int,
                ),
            ])
            exclusion_note = (
                f"{minimum_exclusion_area_text}m²未満のラインは0m²"
                if has_below_minimum_area else ""
            )
            right_rows.append(("除地面積：", exclusion_area_parts, exclusion_note))
            self.append_output_log(
                f"除地面積: {self.format_area_decimal(exclusion_area)}m²"
                f" ≃ {self.format_area_int(exclusion_area)}m²"
            )

        if work_polygons and exclusion_terms:
            result_area_ha = self.format_area_ha(result_area)
            result_area_parts = [
                self.latex_quantity(
                    work_area, r"\mathrm{m}^{2}", self.format_area_int
                ),
                ("−", "-"),
                self.latex_quantity(
                    exclusion_area, r"\mathrm{m}^{2}", self.format_area_int
                ),
                ("=", "="),
                self.latex_quantity(
                    result_area, r"\mathrm{m}^{2}", self.format_area_int
                ),
                ("≃", r"\simeq"),
                (
                    f"{result_area_ha}ha",
                    rf"\color{{#d00000}}{{{result_area_ha}\,\mathrm{{ha}}}}",
                ),
            ]
            right_rows.append(("面積：", result_area_parts, ""))
            self.append_output_log(
                f"面積: {self.format_area_int(work_area)}m² - "
                f"{self.format_area_int(exclusion_area)}m² = "
                f"{self.format_area_int(result_area)}m² ≃ {result_area_ha}ha"
            )

        if not write_to_html:
            return

        left_boxes = root.xpath("//*[@id='calc_distance']")
        right_boxes = root.xpath("//*[@id='calc_area']")
        left_box = left_boxes[0] if left_boxes else None
        right_box = right_boxes[0] if right_boxes else None

        if left_box is not None:
            self.replace_children_with_text(left_box, "")
            for label, parts in left_rows:
                self.append_calc_row(left_box, label, parts, "calc-distance-row")
        if right_box is not None:
            self.replace_children_with_text(right_box, "")
            for label, parts, note in right_rows:
                self.append_calc_row(right_box, label, parts, "calc-area-row", note)

        visible_sides = 0
        if not left_rows and left_box is not None:
            calc.remove(left_box)
        elif left_rows:
            visible_sides += 1
        if not right_rows and right_box is not None:
            calc.remove(right_box)
        elif right_rows:
            visible_sides += 1

        if visible_sides == 0:
            parent = calc.getparent()
            if parent is not None:
                parent.remove(calc)
            return

        calc.set("data-is-jochi-keisan", "1")
        calc.set("data-side-count", str(visible_sides))

    def append_calc_row(self, parent, label, parts, row_class, note=""):
        row = ET.SubElement(parent, "div", **{"class": f"calc-row {row_class}"})
        label_elem = ET.SubElement(row, "div", **{"class": "calc-label"})
        label_elem.text = label
        if note:
            note_elem = ET.SubElement(label_elem, "span", **{"class": "calc-note"})
            note_elem.text = note
        value_elem = ET.SubElement(row, "div", **{"class": "calc-formula"})
        self.apply_latex_parts(value_elem, parts)

    def format_length(self, value):
        return f"{float(value or 0):.1f}"

    def format_area_int(self, value):
        return str(round(float(value or 0)))

    def format_area_decimal(self, value):
        return f"{float(value or 0):.1f}"

    def format_area_ha(self, value):
        return f"{math.trunc((float(value or 0) / 10000) * 100) / 100:.2f}"

    def format_crs_display(self, crs):
        authid = self.clean_html_text(crs.authid()).strip()
        description = self.clean_html_text(crs.description()).strip()
        if authid and description and authid.casefold() != description.casefold():
            return f"{authid} {description}"
        return authid or description

    def latex_escape_text(self, value):
        text = self.clean_html_text(value)
        replacements = {
            "\\": r"\textbackslash{}",
            "{": r"\{", "}": r"\}", "_": r"\_", "%": r"\%",
            "$": r"\$", "#": r"\#", "&": r"\&", "^": r"\^{}",
            "~": r"\~{}",
        }
        return "".join(replacements.get(char, char) for char in text)

    def latex_text(self, value):
        return rf"\text{{{self.latex_escape_text(value)}}}"

    def latex_quantity(self, value, latex_unit, formatter):
        formatted = formatter(value)
        plain_unit = "m²" if "^{2}" in latex_unit else "m"
        return f"{formatted}{plain_unit}", rf"{formatted}\,{latex_unit}"

    def latex_named_quantity(self, name, value, latex_unit):
        plain, latex = self.latex_quantity(value, latex_unit, self.format_area_int)
        if not name:
            return plain, latex
        return f"{name} {plain}", rf"{self.latex_text(name)}\;{latex}"

    def latex_length_term(self, item):
        name = self.clean_html_text(item["name"]).strip()
        quantity = self.latex_quantity(item["length"], r"\mathrm{m}", self.format_length)
        if not name:
            return quantity
        return f"{name} {quantity[0]}", rf"{self.latex_text(name)}\;{quantity[1]}"

    def latex_area_term(self, item):
        name = self.clean_html_text(item["name"]).strip()
        length = self.format_length(item["length"])
        width = self.format_length(item["haba"])
        plain = f"{length}m×{width}m"
        latex = rf"{length}\,\mathrm{{m}}\times{width}\,\mathrm{{m}}"
        if not name:
            return plain, latex
        return f"{name} {plain}", rf"{self.latex_text(name)}\;{latex}"

    def latex_sum_parts(self, terms, total):
        if not terms:
            return [total]
        parts = []
        for index, term in enumerate(terms):
            if index:
                parts.append(("+", "+"))
            parts.append(term)
        parts.extend([("=", "="), total])
        return parts

    def haisui_calc_area(self, item):
        area = self.haisui_raw_area(item)
        return 0 if area < self.minimum_exclusion_area_m2() else area

    def minimum_exclusion_area_m2(self):
        # UIの最小除地面積はa（アール）、計算中の面積はm²。
        return max(0.0, float(self.minEx.value()) * 100.0)

    def haisui_raw_area(self, item):
        return float(item.get("length") or 0) * float(item.get("haba") or 0)

    def haisui_length_term(self, item):
        name = self.clean_html_text(item["name"]).strip()
        length = f"{self.format_length(item['length'])}m"
        return f"{name} {length}" if name else length

    def haisui_area_term(self, item):
        name = self.clean_html_text(item["name"])
        calculation = f"{self.format_length(item['length'])}×{self.format_length(item['haba'])}"
        return f"{name} {calculation}" if name else calculation

    def replace_children_with_text(self, elem, text):
        elem.text = text
        for child in list(elem):
            elem.remove(child)

    def apply_latex_parts(self, elem, parts):
        self.replace_children_with_text(elem, "")
        classes = set((elem.get("class") or "").split())
        classes.add("latex-flow")
        elem.set("class", " ".join(sorted(classes)))
        for plain, latex in parts:
            span = ET.SubElement(
                elem,
                "span",
                **{"class": "latex-part", "data-latex": latex},
            )
            span.text = plain

    def clean_html_tree(self, root):
        for elem in root.iter():
            if elem.text:
                elem.text = elem.text.replace("\r", "")
            if elem.tail:
                elem.tail = elem.tail.replace("\r", "")

    def update_xy_tables(self, root, xy_table_rows, option_panel):
        if not xy_table_rows:
            return

        containers = root.xpath("//*[@id='xy_container']")
        if not containers:
            return

        xy_container = containers[0]
        for table in xy_container.xpath(f".//table[@data-xy-table and @data-option-panel='{option_panel}']"):
            table.getparent().remove(table)

        ordered_rows = list(xy_table_rows)
        order = self.hyouOrder.currentIndex()
        if order in (1, 2):
            ordered_rows.sort(
                key=self.xy_table_row_sort_key,
                reverse=order == 2,
            )

        # 測地系名が2行になっても下枠に収まるよう、1ページ24点にする。
        rows_per_table = 24
        chunks = [
            ordered_rows[i:i + rows_per_table]
            for i in range(0, len(ordered_rows), rows_per_table)
        ]

        for index, rows in enumerate(chunks, start=1):
            table = self.create_xy_table(option_panel, index, rows)
            table.set("data-xy-table", str(index))
            if index != 1:
                table.set("hidden", "hidden")
            xy_container.append(table)

        self.show_xy_updown(root, option_panel, len(chunks))

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

    def xy_table_row_html(self, row):
        if isinstance(row, dict):
            return self.clean_html_text(row.get("html"))
        return self.clean_html_text(row)

    def create_xy_table(self, option_panel, index, rows):
        table = ET.Element(
            "table",
            id=f"xy_table_{option_panel}_{index}",
            **{
                "class": "xy_table",
                "data-option-panel": option_panel,
            }
        )

        thead = ET.SubElement(table, "thead")
        tr = ET.SubElement(thead, "tr")
        for label in ["測点", "X", "Y"]:
            th = ET.SubElement(tr, "th")
            th.text = label

        tbody = ET.SubElement(table, "tbody", id=f"xy_table_body_{option_panel}_{index}")
        rows_root = ET.fromstring(
            f"<tbody>{''.join(self.xy_table_row_html(row) for row in rows)}</tbody>"
        )
        tbody.extend(rows_root)

        return table

    def show_xy_updown(self, root, option_panel, table_count):
        updowns = root.xpath("//*[@id='xy_updown']")
        if not updowns:
            return

        xy_updown = updowns[0]
        xy_updown.attrib.pop("data-option-panel", None)
        if table_count <= 1:
            xy_updown.set("hidden", "hidden")
            return

        xy_updown.attrib.pop("hidden", None)
        xy_updown.set("data-xy-count", str(table_count))

    def update_zumen_options(self, root, type, label_prefix):
        selects = root.xpath("//*[@id='zumen_type']")
        if not selects:
            return

        select = selects[0]

        option = ET.Element("option", value=f"{type}")
        option.text = f"{label_prefix}"
        select.append(option)

    def add_main_map(self, root, option_panel):
        maps = root.xpath("//*[@id='map']")
        if not maps:
            return

        existing = root.xpath(
            f"//*[@id='map']//img[contains(concat(' ', normalize-space(@class), ' '), ' main_map ') and @data-option-panel='{option_panel}']"
        )
        if existing:
            return

        img = ET.Element(
            "img",
            src=f"asset/{option_panel}_map.png",
            alt="",
            **{
                "class": "main_map",
                "data-option-panel": option_panel,
            }
        )
        maps[0].append(img)

    def add_shui_detail(self, root, option_panel, length, area):
        details = root.xpath("//*[@id='xy_container']//*[@id='detail']")
        map_details = root.xpath("//*[@id='map_detail']/dl")
        if details:
            detail = details[0]
            self.add_shui_detail_item(
                detail, f"detail_length_{option_panel}", option_panel,
                "外周全長", self.format_length(length), "m",
                before_id="crs",
            )
            self.add_shui_detail_item(
                detail, f"detail_area_{option_panel}", option_panel,
                "面積", self.format_area_int(area), "m²",
                before_id="crs",
            )
        if map_details:
            self.add_shui_detail_item(
                map_details[0], f"map_area_{option_panel}", option_panel,
                "面積\u00a0", self.format_area_int(area),
                "m²",
                before_id="scale_",
            )

    def add_shui_detail_item(
        self, parent, item_id, option_panel, label, value, suffix, before_id=None
    ):
        if parent.xpath(f"./div[@id='{item_id}']"):
            return
        div = ET.Element(
            "div", id=item_id, **{"data-option-panel": option_panel}
        )
        dt = ET.SubElement(div, "dt", **{"class": "description"})
        dt.text = label
        dd = ET.SubElement(div, "dd", **{"class": "value"})
        span = ET.SubElement(dd, "span")
        span.text = "" if value is None else str(value)
        span.tail = suffix
        before = (
            parent.xpath(f"./div[@id='{before_id}' or .//*[@id='{before_id}']]")
            if before_id else []
        )
        if before:
            parent.insert(parent.index(before[0]), div)
        else:
            parent.append(div)

    def add_haisui_length(self, root, option_panel, length):
        details = root.xpath("//*[@id='map_detail']/dl")
        if not details:
            return

        existing = root.xpath(f"//*[@id='length_{option_panel}']")
        if existing:
            return

        div = ET.Element(
            "div",
            id=f"length_{option_panel}",
            **{"data-option-panel": f"haisui {option_panel}"}
        )
        dt = ET.SubElement(div, "dt", **{"class": "description"})
        dt.text = "延長\xa0"
        dd = ET.SubElement(div, "dd", **{"class": "value"})
        span = ET.SubElement(dd, "span")
        span.text = "" if length is None else str(length)
        span.tail = "m"

        dl = details[0]
        scale = dl.xpath("./div[@id='scale_']")
        if scale:
            dl.insert(dl.index(scale[0]), div)
        else:
            dl.append(div)

    def add_haisui_detail(self, root, option_panel, length, haba):
        details = root.xpath("//*[@id='xy_container']//*[@id='detail']")
        if not details:
            return

        detail = details[0]
        self.add_haisui_detail_item(
            detail,
            f"detail_length_{option_panel}",
            option_panel,
            "延長",
            length,
            ".//span[@id='area']/ancestor::div[1]",
        )
        if haba is not None:
            self.add_haisui_detail_item(
                detail,
                f"detail_haba_{option_panel}",
                option_panel,
                "幅",
                haba,
                ".//*[@id='crs']/ancestor::div[1]",
            )

    def add_haisui_detail_item(self, detail, item_id, option_panel, label, value, before_xpath):
        if detail.xpath(f"./div[@id='{item_id}']"):
            return

        div = ET.Element(
            "div",
            id=item_id,
            **{"data-option-panel": f"haisui {option_panel}"}
        )
        dt = ET.SubElement(div, "dt", **{"class": "description"})
        dt.text = label
        dd = ET.SubElement(div, "dd", **{"class": "value"})
        span = ET.SubElement(dd, "span")
        span.text = "" if value is None else str(value)
        span.tail = "" if value is None else "m"

        before = detail.xpath(before_xpath)
        if before:
            detail.insert(detail.index(before[0]), div)
        else:
            detail.append(div)

    def add_map_name(self, root, option_panel, name, panel_type):
        locations = root.xpath("//*[@id='location']/div[contains(concat(' ', normalize-space(@class), ' '), ' value ')]")
        if not locations:
            return

        item_id = f"{panel_type}_name_{option_panel}"
        if root.xpath(f"//*[@id='{item_id}']"):
            return

        div = ET.Element(
            "div",
            id=item_id,
            **{
                "class": "value3",
                "data-option-panel": f"{panel_type} {option_panel}",
            }
        )
        div.text = "" if name is None else str(name)
        locations[0].append(div)

    def validate_measurement_label_expressions(self, values, item_name):
        expressions = values.get("sokuten_label_expressions", [])
        primary_index = values.get("primary_label_index", 0)
        if not isinstance(expressions, list) or not expressions:
            QMessageBox.warning(self, "エラー", f"{item_name}: 測点名を指定してください")
            return False
        if primary_index < 0 or primary_index >= len(expressions):
            QMessageBox.warning(self, "エラー", f"{item_name}: プライマリー測点名を指定してください")
            return False

        for index, expression_text in enumerate(expressions, start=1):
            expression_text = self.clean_html_text(expression_text).strip()
            if not expression_text:
                QMessageBox.warning(
                    self,
                    "エラー",
                    f"{item_name}: 測点名 {index} の式を入力してください"
                )
                return False
            expression = QgsExpression(expression_text)
            if expression.hasParserError():
                QMessageBox.warning(
                    self,
                    "エラー",
                    f"{item_name}: 測点名 {index} の式が正しくありません:\n"
                    f"{expression.parserErrorString()}"
                )
                return False
        return True

    def validate_inputs(self):
        if (
            self.isShui.isChecked()
        ):
            if not self.shuis:
                QMessageBox.warning(
                    self,
                    "エラー",
                    "少なくとも1つのポリゴンを追加してください"
                )
                return

            names = set()
            for page in self.shuis:
                values = page.values()
                name = values.get("name", "").strip()
                layer = values.get("point_layer")
                if name and name in names:
                    QMessageBox.warning(self, "エラー", f"ポリゴン名が重複しています: {name}")
                    return
                if name:
                    names.add(name)
                if layer is None:
                    QMessageBox.warning(self, "エラー", f"ポリゴン {page.index}: レイヤを選択してください")
                    return
                if layer.type() != QgsMapLayerType.VectorLayer or QgsWkbTypes.geometryType(
                    layer.wkbType()
                ) != QgsWkbTypes.PointGeometry:
                    QMessageBox.warning(self, "エラー", f"ポリゴン {page.index}: ポイントレイヤのみ使用できます")
                    return
                if not self.validate_measurement_label_expressions(
                    values,
                    f"ポリゴン {page.index}",
                ):
                    return
        if (
            self.isHaisui.isChecked()
        ):
            if not self.haisuis:
                QMessageBox.warning(
                    self,
                    "エラー",
                    "少なくとも1つのラインを追加してください"
                )
                return

            # 各ラインページについて、ポイントレイヤと属性指定のバリデーションを行う
            for page in self.haisuis:
                values = page.values()
                layer = values.get("point_layer")
                if layer is None:
                    QMessageBox.warning(
                        self,
                        "エラー",
                        f"ライン {page.index}: レイヤを選択してください"
                    )
                    return

                if layer.type() != QgsMapLayerType.VectorLayer or QgsWkbTypes.geometryType(
                    layer.wkbType()
                ) != QgsWkbTypes.PointGeometry:
                    QMessageBox.warning(
                        self,
                        "エラー",
                        f"ライン {page.index}: ポイントレイヤのみ使用できます"
                    )
                    return

                if not self.validate_measurement_label_expressions(
                    values,
                    f"ライン {page.index}",
                ):
                    return
            
        if not self.isShui.isChecked() and not self.isHaisui.isChecked():
            QMessageBox.warning(
                self,
                "エラー",
                "製図種別を選択してください"
            )
            return
        
        crs_ck = self.crs.crs()
        if not crs_ck.isValid():
            QMessageBox.warning(
                self,
                "エラー",
                "有効な座標参照系を選択してください"
            )
            return

        if (
            crs_ck.isGeographic()
            or crs_ck.mapUnits() != QgsUnitTypes.DistanceMeters
        ):
            QMessageBox.warning(
                self,
                "エラー",
                "長さと面積を正しく計算するため、"
                "平面直角座標系などのメートル単位CRSを指定してください"
            )
            return
        
        path = self.fileName.filePath()

        if not path:
            QMessageBox.warning(
                self,
                "エラー",
                "保存先を指定してください"
            )
            return

        if self.isSaveConfig.currentIndex() == 2 and not self.saveConfig.filePath().strip():
            QMessageBox.warning(
                self,
                "エラー",
                "「ファイルを指定して生成」を選んだ場合は、設定保存ファイルを指定してください"
            )
            return

        p = Path(path)

        # フォルダ存在確認
        if not p.exists() or not p.is_dir():
            QMessageBox.warning(
                self,
                "エラー",
                "保存先には存在するフォルダを指定してください"
            )
            return

        return True
    
    def load_model(self, name):

        path = os.path.join(
            os.path.dirname(__file__),
            "models",
            name
        )

        model = QgsProcessingModelAlgorithm()
        model.fromFile(path)

        return model

    def configure_point_labeling(self, layer, expressions, primary_index):
        expressions = [self.clean_html_text(value).strip() for value in expressions]
        active_indexes = [index for index, value in enumerate(expressions) if value]
        if primary_index not in active_indexes:
            raise ValueError("プライマリー測点名の式が空欄です")

        template_labeling = layer.labeling()
        base_settings = (
            template_labeling.settings()
            if template_labeling is not None
            else QgsPalLayerSettings()
        )
        root_rule = QgsRuleBasedLabeling.Rule(None)
        ordered_indexes = [primary_index] + [
            index for index in active_indexes if index != primary_index
        ]

        secondary_number = 0
        for index in ordered_indexes:
            is_primary = index == primary_index
            settings = QgsPalLayerSettings(base_settings)
            settings.fieldName = expressions[index]
            settings.isExpression = True
            settings.priority = 10 if is_primary else max(1, 8 - secondary_number)
            settings.dist = 0.5 if is_primary else 3.0 + secondary_number * 2.5
            settings.distUnits = Qgis.RenderUnit.Millimeters
            description = "プライマリー測点名" if is_primary else f"測点名 {index + 1}"
            root_rule.appendChild(
                QgsRuleBasedLabeling.Rule(settings, 0, 0, "", description)
            )
            if not is_primary:
                secondary_number += 1

        layer.setLabeling(QgsRuleBasedLabeling(root_rule))
        layer.setLabelsEnabled(True)
        layer.triggerRepaint()
    
    def map_make(self, test=False):
        self._editable_projects = EditableProjects(self.output_dir() / "qgz") if not test else None
        if self.isShui.isChecked():
            total_length = 0.0
            total_area = 0
            self.xy_table_rows = []
            for page in self.shuis:
                values = page.values()
                page.pt_layer = None
                page.line_layer = None
                page.area_layer = None
                page.length = None
                page.area = None
                page.xy_table_rows = []
                make_layer = self.load_model("make_layer.model3")
                params = {
                    'crs': self.crs.crs(),
                    'gpx': values.get('point_layer'),
                    'gpx_extract': values.get('filter_exp') or True,
                    'order': values.get('sort_exp'),
                    'shui_pt': 'memory:',
                    'shui': 'memory:',
                    'area': 'memory:'
                }

                try:
                    result = processing.run(make_layer, params)
                    page.pt_layer = result['shui_pt']
                    page.line_layer = result['shui']
                    page.area_layer = result['area']
                    display_name = self.clean_html_text(values.get('name')).strip() or str(page.index)
                    page.pt_layer.setName(f"ポリゴン_{page.index}_{display_name}_点")
                    page.line_layer.setName(f"ポリゴン_{page.index}_{display_name}_線")
                    point_count = page.pt_layer.featureCount()
                    if point_count < 3:
                        raise ValueError(
                            f"抽出後のポリゴンポイントは{point_count}点です。"
                            "ポリゴン図の作成には3点以上必要です"
                        )
                    page.pt_layer.loadNamedStyle(os.path.join(os.path.dirname(__file__), "styles", "point.qml"))
                    page.line_layer.loadNamedStyle(os.path.join(os.path.dirname(__file__), "styles", "line.qml"))
                    page.length = round(sum(f.geometry().length() for f in page.line_layer.getFeatures()), 1)
                    page.area = int(sum(f.geometry().area() for f in page.area_layer.getFeatures()))
                    label_expressions = values.get("sokuten_label_expressions", [])
                    primary_label_index = values.get("primary_label_index", 0)
                    self.configure_point_labeling(
                        page.pt_layer,
                        label_expressions,
                        primary_label_index,
                    )
                    primary_label_expression = label_expressions[primary_label_index]
                    page.xy_table_rows = self.point_layer_to_html_rows(
                        page.pt_layer,
                        name_expression=primary_label_expression,
                    )
                    self.xy_table_rows.extend(self.point_layer_to_html_rows(
                        page.pt_layer,
                        name_expression=primary_label_expression,
                    ))
                    total_length += page.length
                    total_area += page.area
                except Exception as e:
                    QMessageBox.warning(
                        self,
                        "エラー",
                        f"ポリゴン {self.clean_html_text(values.get('name')).strip() or page.index} "
                        f"の作成に失敗しました:\n{e}"
                    )
                    return False

                if not test:
                    layout = self.create_layout(target_layers=[page.pt_layer, page.line_layer])
                    if layout is None:
                        return False
                    if not self.export_layout_image(layout, f"shui_{page.index}"):
                        return False

            self.htmlValues['shui_length'] = round(total_length, 1)
            self.htmlValues['area'] = int(total_area)
            self.htmlValues['area2'] = self.htmlValues['area']
            self.htmlValues['area_ha'] = self.format_area_ha(self.htmlValues['area'])
        
        if self.isHaisui.isChecked():
            for page in self.haisuis:
                values = page.values()
                page.pt_layer = None
                page.line_layer = None

                make_layer = self.load_model("make_layer_haisui.model3")

                params = {
                    'crs': self.crs.crs(),
                    'gpx': values.get('point_layer'),
                    'gpx_extract': values.get('filter_exp') or True,
                    'order': values.get('sort_exp'),
                    'haisui_pt': 'memory:',
                    'haisui': 'memory:',
                }

                try:
                    result = processing.run(make_layer, params)
                    page.pt_layer = result['haisui_pt']
                    page.line_layer = result['haisui']
                    display_name = self.clean_html_text(values.get('name')).strip() or str(page.index)
                    page.pt_layer.setName(f"ライン_{page.index}_{display_name}_点")
                    page.line_layer.setName(f"ライン_{page.index}_{display_name}_線")
                    point_count = page.pt_layer.featureCount()
                    if point_count < 2:
                        raise ValueError(
                            f"抽出後のポイントは{point_count}点です。"
                            "線の作成には2点以上必要です"
                        )
                    page.pt_layer.loadNamedStyle(os.path.join(os.path.dirname(__file__), "styles", "point.qml"))
                    label_expressions = values.get("sokuten_label_expressions", [])
                    primary_label_index = values.get("primary_label_index", 0)
                    self.configure_point_labeling(
                        page.pt_layer,
                        label_expressions,
                        primary_label_index,
                    )
                    page.line_layer.loadNamedStyle(os.path.join(os.path.dirname(__file__), "styles", "line.qml"))
                    length = round(sum(f.geometry().length() for f in page.line_layer.getFeatures()), 1)
                    page.length = length
                    page.xy_table_rows = self.point_layer_to_html_rows(
                        page.pt_layer,
                        name_expression=label_expressions[primary_label_index],
                    )
                except Exception as e:
                    QMessageBox.warning(
                        self,
                        "エラー",
                        f"ライン {page.index} の作成に失敗しました:\n{e}"
                    )
                    return False

                if not test:
                    layout = self.create_layout(
                        target_layers=[page.pt_layer, page.line_layer]
                    )
                    if layout is None:
                        return False
                    if not self.export_layout_image(layout, f"haisui_{page.index}"):
                        return False

        if not test and (self.isShui.isChecked() or self.isHaisui.isChecked()):
            perimeter_layers = []
            drainage_layers = []
            perimeter_label_layer = None
            if self.isShui.isChecked():
                for page in self.shuis:
                    if page.pt_layer is not None and page.line_layer is not None:
                        self.configure_mix_line_label(page.line_layer, "", QColor("black"))
                        perimeter_layers.extend([page.pt_layer, page.line_layer])

                perimeter_label_layer = self.create_mix_perimeter_label_layer(self.shuis)

            if self.isHaisui.isChecked():
                for page in self.haisuis:
                    if page.pt_layer is None or page.line_layer is None:
                        continue
                    page.pt_layer.loadNamedStyle(os.path.join(os.path.dirname(__file__), "styles", "mix_haisui_point.qml"))
                    line_color = QColor("black") if not self.isShui.isChecked() else None
                    values = page.values()
                    line_label = " ".join(
                        part for part in (
                            self.clean_html_text(values.get("type")).strip(),
                            self.clean_html_text(values.get("name")).strip(),
                        ) if part
                    )
                    self.configure_mix_line_label(
                        page.line_layer,
                        line_label,
                        line_color,
                    )
                    if not self.isShui.isChecked():
                        self.apply_layer_color(
                            page.pt_layer,
                            QColor("black"),
                            include_labels=True,
                        )
                    drainage_layers.extend([page.pt_layer, page.line_layer])

            # QgsLayoutItemMapは先頭のレイヤーほど手前に描画する。
            # ポリゴン名は最前面に残し、ラインの点・線をポリゴンの点・線より手前にする。
            target_layers = (
                ([perimeter_label_layer] if perimeter_label_layer is not None else [])
                + drainage_layers
                + perimeter_layers
            )

            layout = self.create_layout(target_layers=target_layers)
            if layout is None:
                return False
            if not self.export_layout_image(layout, "mix"):
                return False

        if test:
            self.append_output_log(
                "試算のため地図PNG、位置図PDF、GeoPackageは書き込みません"
            )
            return True

        if self.isIchizu.isChecked() and not self.export_location_pdf():
            return False

        if not self.save_result_layers_to_geopackage():
            return False

        return True

    def export_location_pdf(self):
        line_layers = self.location_line_layers()
        if not line_layers:
            return True

        style_dir = Path(__file__).parent / "styles"
        if self.isShui.isChecked():
            for page in self.shuis:
                if page.line_layer is not None:
                    page.line_layer.loadNamedStyle(str(style_dir / "location_shui.qml"))

        if self.isHaisui.isChecked():
            for page in self.haisuis:
                if page.line_layer is not None:
                    page.line_layer.loadNamedStyle(str(style_dir / "location_haisui.qml"))
                    if not self.isShui.isChecked():
                        self.apply_layer_color(page.line_layer, QColor("red"))

        template_path = style_dir / "location.qpt"
        layout = self.create_location_layout_from_template(template_path)
        if layout is None:
            return False

        self.set_location_picture_paths(layout, style_dir)
        if not self.set_location_label_text(layout):
            return False

        map_item = layout.itemById("地図 1")
        if not isinstance(map_item, QgsLayoutItemMap):
            map_item = self.first_layout_map_item(layout)

        if map_item is None:
            QMessageBox.warning(
                self,
                "エラー",
                "位置図テンプレート内に地図アイテムがありません"
            )
            return False

        map_item.setCrs(self.crs.crs())
        map_item.setLayers(self.location_map_layers(line_layers))
        map_item.setKeepLayerSet(True)

        extent = self.combined_layer_extent(line_layers)
        if extent is not None:
            map_item.zoomToExtent(extent)
            map_item.setScale(self.scale.scale())

        if not self.set_location_scale_bars(layout, map_item):
            return False

        self.htmlValues["rinshohan"] = self.rinshohan.text()
        safe_rinshohan = self.safe_file_name(self.htmlValues["rinshohan"])
        output_path = (
            self.output_dir()
            / f"{safe_rinshohan} - 位置図.pdf"
        )
        exporter = QgsLayoutExporter(layout)
        settings = QgsLayoutExporter.PdfExportSettings()
        result = exporter.exportToPdf(str(output_path), settings)
        displayed_path = self.displayed_output_path(output_path)
        if result != QgsLayoutExporter.Success:
            QMessageBox.warning(
                self,
                "エラー",
                f"位置図PDFの保存に失敗しました:\n{displayed_path}"
            )
            return False

        self.append_output_log(f"位置図PDFを書き込みました: {displayed_path}")
        self._editable_projects.capture(
            layout,
            "location",
            output_path.name,
            persistent_layers=self.editable_result_layers(),
        )
        return True

    def location_line_layers(self):
        layers = []
        # QgsLayoutItemMapは先頭のレイヤーほど手前に描画するため、
        # 位置図でもラインをポリゴンより先に並べる。
        if self.isHaisui.isChecked():
            for page in self.haisuis:
                if page.line_layer is not None:
                    layers.append(page.line_layer)

        if self.isShui.isChecked():
            for page in self.shuis:
                if page.line_layer is not None:
                    layers.append(page.line_layer)

        return layers

    def create_location_layout_from_template(self, template_path):
        try:
            with open(template_path, "r", encoding="utf-8") as f:
                template_xml = f.read()
        except OSError as e:
            QMessageBox.warning(
                self,
                "エラー",
                f"位置図テンプレートを読み込めません:\n{e}"
            )
            return None

        doc = QDomDocument()
        content_result = doc.setContent(template_xml)
        content_ok = content_result[0] if isinstance(content_result, tuple) else content_result
        if not content_ok:
            QMessageBox.warning(
                self,
                "エラー",
                f"位置図テンプレートの形式が正しくありません:\n{template_path}"
            )
            return None

        layout = QgsPrintLayout(QgsProject.instance())
        layout.initializeDefaults()
        layout.loadFromTemplate(doc, QgsReadWriteContext())
        layout.setName("RingyoZumenMaker temporary location map")
        return layout

    def set_location_picture_paths(self, layout, style_dir):
        north_arrow = layout.itemById("方位記号")
        if isinstance(north_arrow, QgsLayoutItemPicture):
            north_arrow.setPicturePath(str(style_dir / "houi2.svg"))

    def set_location_scale_bars(self, layout, map_item):
        scale_bars = [
            item
            for item in layout.items()
            if isinstance(item, QgsLayoutItemScaleBar)
        ]
        if len(scale_bars) != 2:
            QMessageBox.warning(
                self,
                "エラー",
                "位置図テンプレート内のスケールバーが2つではありません"
            )
            return False

        for scale_bar in scale_bars:
            scale_bar.setLinkedMap(map_item)
            scale_bar.update()

        return True

    def set_location_label_text(self, layout):
        title_label = layout.itemById("位置図タイトル")
        if not isinstance(title_label, QgsLayoutItemLabel):
            QMessageBox.warning(
                self,
                "エラー",
                "位置図テンプレート内にタイトルラベルがありません"
            )
            return False

        parts = [
            self.clean_html_text(self.rinshohan.text()).strip(),
            self.clean_html_text(self.sanrinshoyusha.text()).strip(),
        ]
        title_label.setText("　".join(part for part in parts if part))
        title_label.update()
        return True

    def first_layout_map_item(self, layout):
        for item in layout.items():
            if isinstance(item, QgsLayoutItemMap):
                return item
        return None

    def location_map_layers(self, line_layers):
        canvas_layers = []
        if self.iface is not None:
            canvas_layers = list(self.iface.mapCanvas().layers())

        excluded_layers = set(line_layers + self.location_input_point_layers())
        background_layers = [
            layer
            for layer in canvas_layers
            if layer not in excluded_layers
        ]
        return line_layers + background_layers

    def location_input_point_layers(self):
        layers = []
        if self.isShui.isChecked():
            for page in self.shuis:
                layer = page.values().get("point_layer")
                if layer is not None:
                    layers.append(layer)

        if self.isHaisui.isChecked():
            for page in self.haisuis:
                layer = page.values().get("point_layer")
                if layer is not None:
                    layers.append(layer)

        return layers

    def combined_layer_extent(self, layers):
        extent = None
        for layer in layers:
            layer_extent = layer.extent()
            if extent is None:
                extent = layer_extent
            else:
                extent.combineExtentWith(layer_extent)

        if extent is None:
            return None

        if extent.width() <= 0 or extent.height() <= 0:
            extent.grow(max(self.scale.scale() / 100, 1))
            return extent

        extent.grow(max(extent.width(), extent.height()) * 0.2)
        return extent

    def save_result_layers_to_geopackage(self):
        self._editable_projects.save()
        self.append_output_log(
            f"編集用QGZ・GeoPackage・操作説明を書き込みました: "
            f"{self.displayed_output_path(self.output_dir() / 'qgz')}"
        )
        return True

    def editable_result_layers(self):
        layers = []
        for page in [*self.shuis, *self.haisuis]:
            for attribute in ("pt_layer", "line_layer"):
                layer = getattr(page, attribute, None)
                if layer is not None:
                    layers.append(layer)
        return layers

    def safe_gpkg_layer_name(self, value):
        text = self.clean_html_text(value).strip()
        for char in '\\/:*?"<>|':
            text = text.replace(char, "_")
        text = "_".join(text.split())
        return text or "layer"

    def safe_file_name(self, value):
        text = self.clean_html_text(value).strip()
        invalid_chars = set('\\/:*?"<>|')
        text = "".join(
            "_" if char in invalid_chars or ord(char) < 32 else char
            for char in text
        )
        text = text.rstrip(". ")
        return text[:120] or "名称未設定"

    def feature_name_from_expression(self, layer, feature, name_expression):
        name_expression = self.clean_html_text(name_expression).strip()
        if not name_expression:
            return feature["name"]

        expression = QgsExpression(name_expression)
        if expression.hasParserError():
            raise ValueError(f"測点名の式が正しくありません: {expression.parserErrorString()}")

        context = QgsExpressionContext()
        context.appendScopes(QgsExpressionContextUtils.globalProjectLayerScopes(layer))
        context.setFeature(feature)

        value = expression.evaluate(context)
        if expression.hasEvalError():
            raise ValueError(f"測点名の評価に失敗しました: {expression.evalErrorString()}")

        return value

    def point_layer_to_html_rows(self, layer, name_expression=None):
        rows = []
        required_fields = ["x", "y"]
        if not self.clean_html_text(name_expression).strip():
            required_fields.append("name")
        field_names = {field.name() for field in layer.fields()}
        missing_fields = [
            field_name
            for field_name in required_fields
            if field_name not in field_names
        ]
        if missing_fields:
            raise ValueError(f"座標表に必要なフィールドがありません: {', '.join(missing_fields)}")

        for f in layer.getFeatures():
            try:
                raw_name = self.feature_name_from_expression(layer, f, name_expression)
                name = escape(self.clean_html_text(raw_name).strip())
                x = float(f["x"])
                y = float(f["y"])
            except (KeyError, TypeError, ValueError) as e:
                raise ValueError(f"座標表の作成に失敗しました feature id={f.id()}") from e

            rows.append({
                "sort_value": raw_name,
                "html": (
                    f"<tr><td>{name}</td><td>{self.format_coordinate(x)}</td>"
                    f"<td>{self.format_coordinate(y)}</td></tr>"
                ),
            })

        return rows

    def format_coordinate(self, value, decimals=None):
        if decimals is None:
            try:
                decimals = int(self.ketasu.value())
            except (AttributeError, TypeError, ValueError):
                decimals = 4

        if decimals < 0:
            decimals = 0

        factor = 10 ** decimals
        truncated = math.trunc(value * factor) / factor
        return f"{truncated:.{decimals}f}"

    def create_layout(self, target_layers=None):
        layout = QgsPrintLayout(QgsProject.instance())
        layout.initializeDefaults()
        layout.setName("RingyoZumenMaker temporary 150x150 map")

        # ページ設定 150 x 150 mm
        page = layout.pageCollection().page(0)
        page.setPageSize(QgsLayoutSize(150, 150, QgsUnitTypes.LayoutMillimeters))

        # 地図アイテム作成
        map_item = QgsLayoutItemMap(layout)
        map_item.attemptMove(QgsLayoutPoint(0, 0, QgsUnitTypes.LayoutMillimeters))
        map_item.attemptResize(QgsLayoutSize(150, 150, QgsUnitTypes.LayoutMillimeters))
        map_item.setCrs(self.crs.crs())

        # LayerSetのレイヤだけ表示
        if not target_layers:
            QMessageBox.warning(
                self,
                "エラー",
                "レイアウトに表示するレイヤがありません"
            )
            return

        map_item.setLayers(target_layers)
        map_item.setKeepLayerSet(True)

        # 縮尺をwidgetから読む
        scale = self.scale.scale()
        map_item.setScale(scale)

        # レイヤ範囲に合わせる
        extent = target_layers[0].extent()
        for layer in target_layers[1:]:
            extent.combineExtentWith(layer.extent())

        if extent.width() <= 0 or extent.height() <= 0:
            extent.grow(max(scale / 100, 1))

        map_item.zoomToExtent(extent)
        map_item.setScale(scale)  # zoomToExtent後にもう一度縮尺固定

        layout.addLayoutItem(map_item)

        return layout

    def copy_assets(self):
        source_dir = Path(__file__).parent / "html_shinsoku" / "asset"
        output_dir = self.output_dir() / "asset"
        try:
            shutil.copytree(source_dir, output_dir, dirs_exist_ok=True)
        except (OSError, shutil.Error) as e:
            QMessageBox.warning(
                self,
                "エラー",
                f"HTMLアセットのコピーに失敗しました:\n{e}"
            )
            return False

        return True

    def export_layout_image(self, layout, prefix):
        output_dir = self.output_dir() / "asset"
        output_dir.mkdir(parents=True, exist_ok=True)

        output_path = str(output_dir / f"{prefix}_map.png")
        exporter = QgsLayoutExporter(layout)
        settings = QgsLayoutExporter.ImageExportSettings()
        settings.dpi = 300
        layout.renderContext().setDpi(300)

        result = exporter.exportToImage(output_path, settings)
        if result != QgsLayoutExporter.Success:
            QMessageBox.warning(
                self,
                "エラー",
                f"地図画像の保存に失敗しました: "
                f"{self.displayed_output_path(output_path)}"
            )
            return False

        self._editable_projects.capture(
            layout,
            prefix,
            f"asset/{prefix}_map.png",
            persistent_layers=self.editable_result_layers(),
        )
        return True

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
        }


class HaisuiPage(QWidget):
    def __init__(self, index, parent=None):
        super().__init__(parent)

        self.index = index

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

        # 幅
        self.haba_spin = QDoubleSpinBox()
        self.haba_spin.setValue(1.0)
        self.haba_spin.setDecimals(2)
        self.haba_spin.setSuffix(" m")
        layout.addRow("幅（m）", self.haba_spin)

        self.isHaba = QCheckBox("図面に幅を表示")
        self.isHaba.setChecked(True)
        layout.addRow("幅の表示", self.isHaba)

        self.is_jochi = QCheckBox("除地として扱う")
        layout.addRow("除地", self.is_jochi)

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
        return {
            "name": self.name_edit.text(),
            "type": self.type_edit.text(),
            "haba": self.haba_spin.value(),
            "show_haba": self.isHaba.isChecked(),
            "is_jochi": self.is_jochi.isChecked(),
            "point_layer": self.point_layer.currentLayer(),
            "filter_exp": self.filter_exp.expression(),
            "sokuten_label_exp": self.sokuten_labels.primary_expression(),
            "sokuten_label_expressions": label_expressions,
            "primary_label_index": self.sokuten_labels.primary_index(),
            "sort_exp": self.sort_exp.expression(),
        }
