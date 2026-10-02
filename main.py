# -*- coding: utf-8 -*-
"""UAV application-area drawing dock."""
import json
import os
import re
import shutil
import tempfile
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from lxml import etree as ET
from qgis.PyQt import uic
from qgis.PyQt.QtCore import QDate
from qgis.PyQt.QtWidgets import QWidget, QDockWidget, QScrollArea, QSizePolicy, QFileDialog, QMessageBox
from qgis.core import QgsProject, QgsLayoutItemPicture, QgsLayoutItemLabel, QgsLayoutItemScaleBar
from .uav_workflow import UavWorkflow

FORM_CLASS, _ = uic.loadUiType(str(Path(__file__).parent / "uav.ui"))
OUTPUT_MANIFEST_NAME = ".ringyo_zumen_outputs.json"


class Main(UavWorkflow, QDockWidget, FORM_CLASS):
    def __init__(self, parent=None, iface=None):
        super().__init__(parent)
        self.iface = iface
        self.setObjectName("RingyoZumenMakerDockWidget")
        content = QWidget()
        self.setupUi(content)
        main_layout = content.layout()
        main_layout.removeWidget(self.tabWidget)
        tab_scroll_area = QScrollArea(content)
        tab_scroll_area.setWidgetResizable(True)
        tab_scroll_area.setMinimumHeight(0)
        tab_scroll_area.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
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
        self.submit_bt.clicked.connect(lambda: self.on_submit(test=False))
        self.testcalc_bt.clicked.connect(lambda: self.on_submit(test=True))
        self.readConfig.clicked.connect(self.load_config_dialog)
        self.saveConfig.setFilter("設定ファイル (*.config)")
        self.isSaveConfig.currentIndexChanged.connect(self.update_save_config_enabled)
        self.setup_uav_inputs()
        self.update_save_config_enabled()

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

    def commit_staged_output(
        self,
        staging_dir,
        final_output_dir,
        backup_generated=False,
    ):
        staging_dir = Path(staging_dir)
        final_output_dir = Path(final_output_dir)
        rollback_dir = staging_dir / ".rollback"
        staged_files = [
            path
            for path in staging_dir.rglob("*")
            if path.is_file() and rollback_dir not in path.parents
        ]
        current_paths = {
            path.relative_to(staging_dir)
            for path in staged_files
            if path.name != OUTPUT_MANIFEST_NAME
        }
        previous_paths = self.previous_generated_output_paths(final_output_dir)
        previous_existing_paths = {
            path for path in previous_paths
            if self.safe_generated_output_file(final_output_dir, path) is not None
        }

        manifest_paths = (
            current_paths
            if backup_generated
            else current_paths | previous_existing_paths
        )
        manifest_source = staging_dir / OUTPUT_MANIFEST_NAME
        self.write_output_manifest(manifest_source, manifest_paths)
        staged_files.append(manifest_source)

        backup_batch_dir = None
        backed_up = []
        committed = []

        try:
            if backup_generated and previous_existing_paths:
                backup_root = final_output_dir / "backup"
                created_at = datetime.now().strftime("%Y%m%d-%H%M%S")
                backup_batch_dir = backup_root / created_at
                suffix = 2
                while backup_batch_dir.exists():
                    backup_batch_dir = backup_root / f"{created_at}-{suffix}"
                    suffix += 1

                for relative_path in sorted(
                    previous_existing_paths,
                    key=lambda path: path.as_posix(),
                ):
                    source_path = self.safe_generated_output_file(
                        final_output_dir,
                        relative_path,
                    )
                    if source_path is None:
                        continue
                    backup_path = backup_batch_dir / relative_path
                    backup_path.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(source_path, backup_path)
                    backed_up.append((source_path, backup_path))

            for source_path in staged_files:
                relative_path = source_path.relative_to(staging_dir)
                target_path = final_output_dir / relative_path
                target_path.parent.mkdir(parents=True, exist_ok=True)

                backup_path = None
                if target_path.exists():
                    backup_path = rollback_dir / relative_path
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

            for source_path, backup_path in reversed(backed_up):
                try:
                    if backup_path.exists():
                        source_path.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(backup_path, source_path)
                except OSError:
                    pass

            if backup_batch_dir is not None and backup_batch_dir.exists():
                self.remove_empty_generated_directories(
                    backup_batch_dir, previous_existing_paths
                )
                try:
                    backup_batch_dir.rmdir()
                except OSError:
                    pass

            QMessageBox.warning(
                self,
                "エラー",
                f"完成したファイルを出力先へ反映できません:\n{e}"
            )
            return False

        if backup_batch_dir is not None:
            self.remove_empty_generated_directories(
                final_output_dir,
                previous_existing_paths,
            )
            self.append_output_log(
                f"以前の生成ファイルをバックアップしました: {backup_batch_dir}"
            )

        return True

    def previous_generated_output_paths(self, output_dir):
        output_dir = Path(output_dir)
        manifest_path = output_dir / OUTPUT_MANIFEST_NAME
        try:
            with open(manifest_path, "r", encoding="utf-8") as manifest_file:
                data = json.load(manifest_file)
            if (
                isinstance(data, dict)
                and data.get("format") == "RingyoZumenMaker.outputs"
                and isinstance(data.get("paths"), list)
            ):
                return {
                    normalized
                    for value in data["paths"]
                    if (normalized := self.normalized_generated_output_path(value))
                    is not None
                }
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass

        return self.legacy_generated_output_paths(output_dir)

    def legacy_generated_output_paths(self, output_dir):
        output_dir = Path(output_dir)
        paths = set()

        def add_if_file(relative_path):
            relative_path = Path(relative_path)
            if self.safe_generated_output_file(output_dir, relative_path) is not None:
                paths.add(relative_path)

        add_if_file("index.html")
        add_if_file("input.config")

        for pdf_path in output_dir.glob("* - 位置図.pdf"):
            add_if_file(pdf_path.relative_to(output_dir))

        asset_source = Path(__file__).parent / "html_shinsoku" / "asset"
        for source_path in asset_source.rglob("*"):
            if source_path.is_file():
                add_if_file(Path("asset") / source_path.relative_to(asset_source))

        asset_dir = output_dir / "asset"
        map_pattern = re.compile(
            r"(?:shui_\d+|haisui_\d+|shui_all|haisui_all|mix)_map\.png"
        )
        if asset_dir.is_dir():
            for map_path in asset_dir.glob("*_map.png"):
                if map_pattern.fullmatch(map_path.name):
                    add_if_file(map_path.relative_to(output_dir))

        qgz_dir = output_dir / "qgz"
        qgz_pattern = re.compile(
            r"(?:shui_\d+|haisui_\d+|shui_all|haisui_all|mix|location)\.qgz"
        )
        for name in ("ringyo_zumen.gpkg", "houi2.svg", "操作説明.md"):
            add_if_file(Path("qgz") / name)
        if qgz_dir.is_dir():
            for qgz_path in qgz_dir.glob("*.qgz"):
                if qgz_pattern.fullmatch(qgz_path.name):
                    add_if_file(qgz_path.relative_to(output_dir))

        return paths

    @staticmethod
    def normalized_generated_output_path(value):
        if not isinstance(value, str) or not value.strip():
            return None
        path = Path(value)
        if (
            path.is_absolute()
            or path.drive
            or ".." in path.parts
            or path.name == OUTPUT_MANIFEST_NAME
            or (path.parts and path.parts[0].casefold() == "backup")
        ):
            return None
        return path

    def safe_generated_output_file(self, output_dir, relative_path):
        relative_path = self.normalized_generated_output_path(
            Path(relative_path).as_posix()
        )
        if relative_path is None:
            return None

        output_dir = Path(output_dir).resolve()
        target_path = output_dir / relative_path
        try:
            if (
                target_path.is_symlink()
                or not target_path.is_file()
                or not target_path.resolve().is_relative_to(output_dir)
            ):
                return None
        except (OSError, RuntimeError):
            return None
        return target_path

    @staticmethod
    def write_output_manifest(path, generated_paths):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "format": "RingyoZumenMaker.outputs",
            "version": 1,
            "paths": sorted(
                Path(relative_path).as_posix()
                for relative_path in generated_paths
            ),
        }
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        try:
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as manifest_file:
                json.dump(data, manifest_file, ensure_ascii=False, indent=2)
                manifest_file.write("\n")
            os.replace(temporary_name, path)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

    @staticmethod
    def remove_empty_generated_directories(output_dir, relative_paths):
        output_dir = Path(output_dir).resolve()
        candidates = {
            parent
            for relative_path in relative_paths
            for parent in (output_dir / relative_path).parents
            if parent != output_dir and output_dir in parent.parents
        }
        for directory in sorted(candidates, key=lambda path: len(path.parts), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                pass

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

    def update_save_config_enabled(self, *_):
        self.saveConfig.setEnabled(self.isSaveConfig.currentIndex() == 2)

    def selected_config_output_path(self):
        mode = self.isSaveConfig.currentIndex()
        if mode == 0:
            return None
        if mode == 1:
            return self.output_dir() / "input.config"

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

        displayed_path = self.displayed_output_path(path)
        self.append_output_log(f"設定ファイルを書き込みました: {displayed_path}")
        if self.isSaveConfig.currentIndex() == 2:
            self.register_generated_output_path(path)
        return True

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
                "次の入力レイヤは現在のプロジェクトで見つからないため、空欄にしました:\n"
                + "\n".join(missing_layers),
            )
        return True

    def set_date_from_config(self, widget, value):
        date = QDate.fromString(self.clean_html_text(value), "yyyy-MM-dd")
        if date.isValid():
            widget.setDate(date)

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

    def format_decimal(self, value, decimals):
        decimals = max(0, int(decimals))
        number = Decimal(str(value or 0))
        quantum = Decimal(1).scaleb(-decimals)
        rounded = number.quantize(quantum, rounding=ROUND_HALF_UP)
        if rounded == 0:
            rounded = abs(rounded)
        return f"{rounded:.{decimals}f}"

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

    def replace_children_with_text(self, elem, text):
        elem.text = text
        for child in list(elem):
            elem.remove(child)

    def safe_file_name(self, value):
        text = self.clean_html_text(value).strip()
        invalid_chars = set('\\/:*?"<>|')
        text = "".join(
            "_" if char in invalid_chars or ord(char) < 32 else char
            for char in text
        )
        text = text.rstrip(". ")
        return text[:120] or "名称未設定"

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
