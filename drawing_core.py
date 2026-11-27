"""Shared geometry, rendering and transactional exports; no input widgets."""
from pathlib import Path
from contextlib import contextmanager, ExitStack
from datetime import datetime
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from numbers import Number
from html import escape
from time import sleep
import os, json, math, re, shutil, tempfile, unicodedata, uuid
from lxml import etree as ET
from qgis.PyQt.QtCore import QDate, QVariant
from qgis.PyQt.QtGui import QColor, QFont
from qgis.PyQt.QtXml import QDomDocument
from qgis.core import (
    Qgis, QgsWkbTypes, QgsMapLayerType, QgsVectorLayerSimpleLabeling, QgsRuleBasedLabeling,
    QgsProcessingModelAlgorithm, QgsProject, QgsPrintLayout, QgsLayoutItemMap,
    QgsLayoutPoint, QgsLayoutSize, QgsUnitTypes, QgsLayoutExporter, QgsPalLayerSettings,
    QgsExpression, QgsExpressionContext, QgsExpressionContextUtils, QgsLayoutItemLabel,
    QgsLayoutItemPicture, QgsLayoutItemScaleBar, QgsCoordinateReferenceSystem,
    QgsVectorFileWriter, QgsReadWriteContext, QgsFeature, QgsField, QgsGeometry, QgsVectorLayer,
)
import processing
from .editable_projects import EditableProjects
from .unified_project import full_input, metadata, checkpoint_package
from .drawing_inputs import FIELDS, DrawingError, DrawingCancelled
from .polygon_geometry import reconstruct_polygon_layers, validate_polygon_vertices
from .map_frame import validate_map_frame, rendered_map_bounds

OUTPUT_MANIFEST_NAME = ".ringyo_zumen_outputs.json"


def replace_output_file(source, destination):
    for delay in (0.1, 0.25, 0.5, None):
        try:
            os.replace(source, destination)
            return
        except OSError as error:
            if getattr(error, 'winerror', None) not in (32, 33):
                raise
            if delay is None:
                raise OSError(f"ファイルを更新できません。表示中のPDFやQGISで開いている出力ファイルを閉じてから再実行してください。\n{error}") from error
            sleep(delay)


class _Errors:
    @staticmethod
    def warning(owner, title, message):
        raise DrawingError(message)

QMessageBox = _Errors


class DrawingOperations:
    @contextmanager
    def export_layer_context_for_project(self, project):
        with ExitStack() as stack:
            for layout in project.layoutManager().layouts():
                stack.enter_context(self.export_layer_context(layout))
            yield

    @staticmethod
    def resize_location_layout(layout, factor):
        page = layout.pageCollection().page(0)
        size = page.pageSize()
        items = []
        for item in layout.items():
            if not hasattr(item, "positionWithUnits") or item is page:
                continue
            items.append((item, item.positionWithUnits(), item.sizeWithUnits()))
        page.setPageSize(QgsLayoutSize(size.width() * factor, size.height() * factor))
        for item, position, dimensions in items:
            scale = item.scale() if isinstance(item, QgsLayoutItemMap) else None
            item.attemptResize(QgsLayoutSize(dimensions.width() * factor, dimensions.height() * factor))
            if scale is not None:
                item.setScale(scale)
            item.attemptMove(QgsLayoutPoint(position.x() * factor, position.y() * factor))

    def execute(self, test=False):
        self.success = False
        self.open_output_tab()
        self.clear_output_log()

        if not self.validate_inputs():
            return

        self.progressBar.setValue(0)

        final_output_dir = Path(self.fileName.filePath())
        staging_dir = None
        snapshot_directory = None
        closed = False
        original_project = QgsProject.instance().fileName()
        self._generated_scales = {}
        self._map_content_bounds = {}
        self._project_snapshot = None
        temporary_config = final_output_dir / "input.tmp.config"

        try:
            if not test:
                if not self.save_config_file(temporary_config):
                    return
                if self.outputMode.currentIndex() == 0:
                    if not self.settle_project_edits():
                        return
                    if self.preserve_active_project and metadata(QgsProject.instance()):
                        snapshot_directory = tempfile.TemporaryDirectory(prefix="measurement-snapshot-")
                        self._project_snapshot = self.snapshot_project(Path(snapshot_directory.name) / "snapshot.qgz")
                    elif self.preserve_active_project:
                        previous_project = final_output_dir / 'qgz' / self.project_file_name()
                        if previous_project.is_file():
                            self._project_snapshot = previous_project
            if not test:
                staging_dir = Path(tempfile.mkdtemp(
                    prefix=".ringyo_zumen_tmp_",
                    dir=final_output_dir,
                ))
                self._output_dir_override = staging_dir
                self._final_output_dir = final_output_dir

            self.LayerSet = {}
            self.xy_table_rows = []
            self.line_xy_table_rows = []
            self.total_line_length = 0.0
            self.htmlValues = {}
            self.progressBar.setValue(5)

            if not self.map_make(test=test):
                self.progressBar.setValue(0)
                return

            if test:
                if not self.make_html(write_file=False):
                    self.progressBar.setValue(0)
                    return
                self.progressBar.setValue(100)
                self.success = True
                return

            self.progressBar.setValue(85)

            if not self.copy_assets():
                self.progressBar.setValue(0)
                return
            self.progressBar.setValue(90)

            if not self.make_html():
                self.progressBar.setValue(0)
                return

            # 出力フォルダへ自動生成する設定ファイルも、他の生成物と
            # 同じトランザクションとバックアップ対象に含める。
            if not self.save_config_file():
                self.progressBar.setValue(0)
                return

            self.progressBar.setValue(95)
            if self.preserve_active_project and self.outputMode.currentIndex() == 0 and metadata(QgsProject.instance()):
                QgsProject.instance().clear()
                closed = True
            if self.outputMode.currentIndex() == 0:
                checkpoint_package(final_output_dir / "qgz" / "ringyo_zumen.gpkg")
            if not self.commit_staged_output(
                staging_dir,
                final_output_dir,
                backup_generated=self.backupQgz.isChecked(),
            ):
                self.progressBar.setValue(0)
                return

            if closed:
                project_path = final_output_dir / "qgz" / self.project_file_name()
                if not QgsProject.instance().read(str(project_path)):
                    raise OSError("更新したQGZを開けません: " + str(project_path))
                closed = False
            temporary_config.unlink(missing_ok=True)

            self.append_output_log(f"出力を確定しました: {final_output_dir}")
            self.progressBar.setValue(100)
            self.success = True
        except DrawingCancelled:
            raise
        except Exception as e:
            QMessageBox.warning(
                self,
                "エラー",
                f"出力処理に失敗しました:\n{e}"
            )
            self.progressBar.setValue(0)
        finally:
            if closed and self._project_snapshot:
                QgsProject.instance().read(str(self._project_snapshot))
                QgsProject.instance().setFileName(original_project)
            self._editable_projects = None
            self._project_snapshot = None
            if snapshot_directory is not None:
                snapshot_directory.cleanup()
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

    def commit_staged_output(
        self,
        staging_dir,
        final_output_dir,
        backup_generated=False,
    ):
        staging_dir = Path(staging_dir)
        final_output_dir = Path(final_output_dir)
        rollback_dir = staging_dir / ".rollback"
        manifest_source = staging_dir / OUTPUT_MANIFEST_NAME
        staged_files = [
            path
            for path in staging_dir.rglob("*")
            if path.is_file() and path != manifest_source and rollback_dir not in path.parents
        ]
        current_paths = {path.relative_to(staging_dir) for path in staged_files}
        previous_paths = self.previous_generated_output_paths(final_output_dir)
        previous_existing_paths = {
            path for path in previous_paths
            if self.safe_generated_output_file(final_output_dir, path) is not None
        }

        retained = {p for p in previous_existing_paths if p.parts and p.parts[0] == "qgz"} if self.outputMode.currentIndex() == 1 else set()
        previous_existing_paths -= retained
        manifest_paths = retained | current_paths
        self.write_output_manifest(manifest_source, manifest_paths)
        staged_files.append(manifest_source)

        backup_batch_dir = None
        backed_up = []
        committed = []

        try:
            if not backup_generated:
                # Retire only manifest-owned obsolete files, with the same rollback
                # protection as replacements. User-added files are not enumerated.
                for relative_path in sorted(previous_existing_paths - current_paths):
                    target_path = self.safe_generated_output_file(final_output_dir, relative_path)
                    if target_path is None:
                        continue
                    backup_path = rollback_dir / relative_path
                    backup_path.parent.mkdir(parents=True, exist_ok=True)
                    replace_output_file(target_path, backup_path)
                    committed.append({"target": target_path, "backup": backup_path, "installed": False})
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
                    replace_output_file(source_path, backup_path)
                    backed_up.append((source_path, backup_path))

            for source_path in staged_files:
                relative_path = source_path.relative_to(staging_dir)
                target_path = final_output_dir / relative_path
                target_path.parent.mkdir(parents=True, exist_ok=True)

                backup_path = None
                if target_path.exists():
                    backup_path = rollback_dir / relative_path
                    backup_path.parent.mkdir(parents=True, exist_ok=True)
                    replace_output_file(target_path, backup_path)

                record = {
                    "target": target_path,
                    "backup": backup_path,
                    "installed": False,
                }
                committed.append(record)
                replace_output_file(source_path, target_path)
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
                shutil.rmtree(backup_batch_dir, ignore_errors=True)

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

    def save_config_file(self, path=None):
        path = Path(path) if path is not None else self.output_dir() / "input.config"
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
        return True

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
            settings.labelPerPart = False
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
                elem.set("title", self.clean_html_text(value))

        title = root.xpath("//title")[0]
        title.text = ' - '.join(filter(None, (self.clean_html_text(self.htmlValues['rinshohan']).strip(), '実測図')))

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
                displayed_haba = values.get("haba") if values.get("has_haba") else None
                self.add_haisui_detail(
                    root,
                    f"haisui_{page.index}",
                    page.length,
                    displayed_haba,
                )
                if name_text:
                    self.add_map_name(
                        root, f"haisui_{page.index}", name_text, "haisui"
                    )

        if self.isShui.isChecked():
            self.update_zumen_options(root, "shui_all", "全体（ポリゴン）")
            self.update_xy_tables(root, self.xy_table_rows, "shui_all")
            self.add_main_map(root, "shui_all")
            self.add_shui_detail(
                root,
                "shui_all",
                self.htmlValues["shui_length"],
                self.htmlValues["area"],
            )

        if self.isHaisui.isChecked():
            self.update_zumen_options(root, "haisui_all", "全体（ライン）")
            self.update_xy_tables(root, self.line_xy_table_rows, "haisui_all")
            self.add_main_map(root, "haisui_all")
            self.add_haisui_detail(
                root,
                "haisui_all",
                self.total_line_length,
                None,
            )
            self.add_haisui_length(root, "haisui_all", self.total_line_length)

        if self.isShui.isChecked() and self.isHaisui.isChecked():
            self.update_zumen_options(root, "mix", "全体")
            self.update_xy_tables(root, self.xy_table_rows, "mix")
            self.add_main_map(root, "mix")

        calc_panels = root.xpath("//*[@id='calc']")
        if calc_panels:
            if self.isShui.isChecked() and self.isHaisui.isChecked():
                calc_panels[0].set("data-option-panel", "mix")
            elif self.isShui.isChecked():
                calc_panels[0].set("data-option-panel", "shui_all")
            elif self.isHaisui.isChecked():
                calc_panels[0].set("data-option-panel", "haisui_all")

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
        head = root.xpath("//head")[0]
        style = ET.SubElement(head, "style")
        paper = self.paper.currentText()
        style.text = f"@page {{ size: {paper} landscape; margin: 0; }}"
        root.xpath("//body")[0].set("data-paper", paper)
        root.xpath("//*[@id='main_container']")[0].set("style", f"--paper-factor:{self.paper_factor()}")
        for panel, scale in getattr(self, "_generated_scales", {}).items():
            for node in root.xpath("//*[@id='scale']/*[@data-option-panel]"):
                if panel in node.get("data-option-panel", "").split():
                    node.text = f"1/{scale:.0f}"
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

    def log_calculation_detail(self, message):
        self.append_output_log(message)

    def calculation_feedback(self):
        return getattr(self, 'feedback', None)

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
            total_text = self.format_length(total)
            parts = self.latex_sum_parts(
                [self.latex_length_term(item) for item in typed_items],
                self.latex_quantity(total, r"\mathrm{m}", self.format_length),
                self.displayed_sum_symbol(
                    [self.format_length(item["length"]) for item in typed_items],
                    total_text,
                ),
            )
            left_rows.append((f"{line_type}：", parts))
            self.log_calculation_detail(
                f"{line_type}延長: {' + '.join(self.haisui_length_term(item) for item in typed_items)}"
                f" = {self.format_length(total)}m"
            )

        if jochi_lines:
            jochi_length_text = self.format_length(jochi_line_length)
            jochi_length_parts = self.latex_sum_parts(
                [self.latex_length_term(item) for item in jochi_lines],
                self.latex_quantity(
                    jochi_line_length,
                    r"\mathrm{m}",
                    self.format_length,
                ),
                self.displayed_sum_symbol(
                    [self.format_length(item["length"]) for item in jochi_lines],
                    jochi_length_text,
                ),
            )
            left_rows.append(("除地ライン：", jochi_length_parts))
            self.log_calculation_detail(
                "除地ライン延長: "
                + " + ".join(self.haisui_length_term(item) for item in jochi_lines)
                + f" = {self.format_length(jochi_line_length)}m"
            )

        right_rows = []
        if work_polygons:
            work_area_text = self.format_area_int(work_area)
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
                self.displayed_sum_symbol(
                    [self.format_area_int(item["area"]) for item in work_polygons],
                    work_area_text,
                ),
            )
            right_rows.append(("施行地面積：", work_area_parts, ""))
            self.log_calculation_detail(
                f"施行地面積: {self.format_area_int(work_area)}m²"
            )

        exclusion_terms = [
            self.latex_named_quantity(
                item["name"], item["area"], r"\mathrm{m}^{2}"
            )
            for item in jochi_polygons
        ] + [self.latex_area_term(item) for item in jochi_lines]
        if exclusion_terms:
            detailed_exclusion_text = self.format_area_decimal(exclusion_area)
            displayed_exclusion_text = self.format_area_int(exclusion_area)
            displayed_exclusion_terms = [
                self.format_area_int(item["area"]) for item in jochi_polygons
            ] + [
                Decimal(self.format_length(item["length"]))
                * Decimal(self.format_width(item["haba"]))
                for item in jochi_lines
            ]
            detailed_matches_display = (
                Decimal(detailed_exclusion_text)
                == Decimal(displayed_exclusion_text)
            )
            exclusion_total_formatter = (
                self.format_area_int
                if detailed_matches_display
                else self.format_area_decimal
            )
            exclusion_area_parts = self.latex_sum_parts(
                exclusion_terms,
                self.latex_quantity(
                    exclusion_area,
                    r"\mathrm{m}^{2}",
                    exclusion_total_formatter,
                ),
                self.displayed_sum_symbol(
                    displayed_exclusion_terms,
                    (
                        displayed_exclusion_text
                        if detailed_matches_display
                        else detailed_exclusion_text
                    ),
                ),
            )
            if not detailed_matches_display:
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
            self.log_calculation_detail(
                f"除地面積: {self.format_area_decimal(exclusion_area)}m²"
                f" ≃ {self.format_area_int(exclusion_area)}m²"
            )

        if work_polygons:
            result_area_ha = self.format_area_ha(result_area)
            displayed_result_area = self.format_area_int(result_area)
            hectare_symbol = self.displayed_equality_symbol(
                Decimal(displayed_result_area) / Decimal("10000"),
                result_area_ha,
            )
            result_area_parts = []
            if exclusion_terms:
                displayed_work_area = self.format_area_int(work_area)
                displayed_exclusion_area = self.format_area_int(exclusion_area)
                result_area_parts.extend([
                    self.latex_quantity(work_area, r"\mathrm{m}^{2}", self.format_area_int),
                    ("−", "-"),
                    self.latex_quantity(exclusion_area, r"\mathrm{m}^{2}", self.format_area_int),
                    self.displayed_equality_symbol(
                        Decimal(displayed_work_area) - Decimal(displayed_exclusion_area),
                        displayed_result_area,
                    ),
                ])
            result_area_parts.extend([
                self.latex_quantity(
                    result_area, r"\mathrm{m}^{2}", self.format_area_int
                ),
                hectare_symbol,
                (
                    f"{result_area_ha}ha",
                    rf"\color{{#d00000}}{{{result_area_ha}\,\mathrm{{ha}}}}",
                ),
            ])
            right_rows.append(("面積：", result_area_parts, ""))
            self.log_calculation_detail("面積: " + " ".join(plain for plain, _ in result_area_parts))

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
        return self.format_decimal(value, self.lengthDecimals.value())

    def format_area_int(self, value):
        return self.format_decimal(value, self.areaDecimals.value())

    def format_area_decimal(self, value):
        # 近似記号の左側には、指定した表示桁より1桁多く残す。
        return self.format_decimal(value, self.areaDecimals.value() + 1)

    def format_area_ha(self, value):
        decimals = self.haDecimals.value()
        hectares = Decimal(str(value or 0)) / Decimal("10000")
        quantum = Decimal(1).scaleb(-decimals)
        truncated = hectares.quantize(quantum, rounding=ROUND_DOWN)
        if truncated == 0:
            truncated = abs(truncated)
        return f"{truncated:.{decimals}f}"

    def format_decimal(self, value, decimals):
        decimals = max(0, int(decimals))
        number = Decimal(str(value or 0))
        quantum = Decimal(1).scaleb(-decimals)
        rounded = number.quantize(quantum, rounding=ROUND_HALF_UP)
        if rounded == 0:
            rounded = abs(rounded)
        return f"{rounded:.{decimals}f}"

    def format_width(self, value):
        # 幅入力は小数第2位まで保持し、不要な末尾の0だけを省く。
        return self.format_decimal(value, 2).rstrip("0").rstrip(".")

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
        width = self.format_width(item["haba"])
        plain = f"{length}m×{width}m"
        latex = rf"{length}\,\mathrm{{m}}\times{width}\,\mathrm{{m}}"
        if not name:
            return plain, latex
        return f"{name} {plain}", rf"{self.latex_text(name)}\;{latex}"

    def displayed_equality_symbol(self, left_value, right_value):
        left = Decimal(str(left_value))
        right = Decimal(str(right_value))
        return ("=", "=") if left == right else ("≃", r"\simeq")

    def displayed_sum_symbol(self, values, total):
        displayed_sum = sum(
            (Decimal(str(value)) for value in values),
            Decimal("0"),
        )
        return self.displayed_equality_symbol(displayed_sum, total)

    def latex_sum_parts(self, terms, total, equality_symbol=("=", "=")):
        if not terms:
            return [total]
        # 1項だけで丸め後の値が合計と一致する場合は、名前の有無にかかわらず
        # 「L1 517.8m = 517.8m」のような重複を避けて結果だけを表示する。
        if len(terms) == 1 and equality_symbol[0] == "=":
            return [total]
        parts = []
        for index, term in enumerate(terms):
            if index:
                parts.append(("+", "+"))
            parts.append(term)
        parts.extend([equality_symbol, total])
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
        bounds = getattr(self, '_map_content_bounds', {}).get(option_panel)
        if bounds:
            img.set('data-content-bounds', json.dumps(bounds))
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
        span.text = "" if length is None else self.format_length(length)
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
            self.format_length(length),
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
                button = getattr(page.name_edit, '_drawing_override', None)
                data_defined = button.isActive() if button is not None else getattr(page, 'name_is_data_defined', False)
                if name and not data_defined and name in names:
                    QMessageBox.warning(self, "エラー", f"ポリゴン名が重複しています: {name}")
                    return
                if name and not data_defined:
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

        if not path and not getattr(self, '_skip_output_validation', False):
            QMessageBox.warning(
                self,
                "エラー",
                "保存先を指定してください"
            )
            return

        for widget in ([self.scale, self.locationScale] if self.isIchizu.isChecked() else [self.scale]):
            if not math.isfinite(widget.scale()) or widget.scale() <= 0:
                QMessageBox.warning(self, "エラー", "縮尺には正の値を指定してください")
                return False

        p = Path(path)

        # フォルダ存在確認
        if not getattr(self, '_skip_output_validation', False) and (not p.exists() or not p.is_dir()):
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
        self._editable_projects = EditableProjects(self, self.output_dir(), getattr(self, "_project_snapshot", None)) if not test and self.outputMode.currentIndex() == 0 else None
        self.line_xy_table_rows = []
        self.total_line_length = 0.0
        polygon_count = len(self.shuis) if self.isShui.isChecked() else 0
        line_count = len(self.haisuis) if self.isHaisui.isChecked() else 0
        input_count = max(1, polygon_count + line_count)
        if self.isShui.isChecked():
            total_length = 0.0
            total_area = 0
            self.xy_table_rows = []
            for input_index, page in enumerate(self.shuis, 1):
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
                    'gpx': full_input(values.get('point_layer')),
                    'gpx_extract': values.get('filter_exp') or True,
                    'order': values.get('sort_exp'),
                    'part': values.get('part_exp') or '',
                    'shui_pt': 'memory:',
                    'shui': 'memory:',
                    'area': 'memory:'
                }

                try:
                    result = processing.run(make_layer, params, feedback=self.calculation_feedback())
                    page.pt_layer = result['shui_pt']
                    page.line_layer = result['shui']
                    page.area_layer = result['area']
                    validate_polygon_vertices(page.pt_layer, values.get('part_exp', ''))
                    page.line_layer, page.area_layer, exterior_length = reconstruct_polygon_layers(
                        page.line_layer, page.area_layer)
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
                    page.length = exterior_length
                    page.area = sum(
                        f.geometry().area() for f in page.area_layer.getFeatures()
                    )
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
                self.progressBar.setValue(5 + round(40 * input_index / input_count))

            self.htmlValues['shui_length'] = self.format_length(total_length)
            self.htmlValues['area'] = self.format_area_int(total_area)
            self.htmlValues['area2'] = self.htmlValues['area']
            self.htmlValues['area_ha'] = self.format_area_ha(total_area)
        
        if self.isHaisui.isChecked():
            for input_index, page in enumerate(self.haisuis, polygon_count + 1):
                values = page.values()
                page.pt_layer = None
                page.line_layer = None

                make_layer = self.load_model("make_layer_haisui.model3")

                params = {
                    'crs': self.crs.crs(),
                    'gpx': full_input(values.get('point_layer')),
                    'gpx_extract': values.get('filter_exp') or True,
                    'order': values.get('sort_exp'),
                    'haisui_pt': 'memory:',
                    'haisui': 'memory:',
                }

                try:
                    result = processing.run(make_layer, params, feedback=self.calculation_feedback())
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
                    length = sum(
                        f.geometry().length() for f in page.line_layer.getFeatures()
                    )
                    page.length = length
                    page.xy_table_rows = self.point_layer_to_html_rows(
                        page.pt_layer,
                        name_expression=label_expressions[primary_label_index],
                    )
                    self.line_xy_table_rows.extend(page.xy_table_rows)
                    self.total_line_length += page.length
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
                self.progressBar.setValue(5 + round(40 * input_index / input_count))

        if not test and self.isShui.isChecked():
            perimeter_layers = []
            for page in self.shuis:
                if page.pt_layer is not None and page.line_layer is not None:
                    self.configure_mix_line_label(page.line_layer, "", QColor("black"))
                    perimeter_layers.extend([page.pt_layer, page.line_layer])

            perimeter_label_layer = self.create_mix_perimeter_label_layer(self.shuis)
            target_layers = (
                ([perimeter_label_layer] if perimeter_label_layer is not None else [])
                + perimeter_layers
            )
            layout = self.create_layout(target_layers=target_layers)
            if layout is None:
                return False
            if not self.export_layout_image(layout, "shui_all"):
                return False

        if not test and self.isHaisui.isChecked():
            line_layers = []
            for page in self.haisuis:
                if page.pt_layer is None or page.line_layer is None:
                    continue
                values = page.values()
                page.pt_layer.loadNamedStyle(os.path.join(os.path.dirname(__file__), "styles", "mix_haisui_point.qml"))
                self.apply_layer_color(
                    page.pt_layer,
                    QColor("black"),
                )
                self.configure_point_labeling(
                    page.pt_layer,
                    values.get("sokuten_label_expressions", []),
                    values.get("primary_label_index", 0),
                )
                self.configure_mix_line_label(
                    page.line_layer,
                    self.clean_html_text(values.get("name")).strip(),
                    QColor("black"),
                )
                line_layers.extend([page.pt_layer, page.line_layer])

            layout = self.create_layout(target_layers=line_layers)
            if layout is None:
                return False
            if not self.export_layout_image(layout, "haisui_all"):
                return False

        if not test and self.isShui.isChecked() and self.isHaisui.isChecked():
            perimeter_layers = []
            drainage_layers = []
            for page in self.shuis:
                if page.pt_layer is not None and page.line_layer is not None:
                    self.configure_mix_line_label(page.line_layer, "", QColor("black"))
                    perimeter_layers.extend([page.pt_layer, page.line_layer])

            perimeter_label_layer = self.create_mix_perimeter_label_layer(self.shuis)

            for page in self.haisuis:
                if page.pt_layer is None or page.line_layer is None:
                    continue
                values = page.values()
                page.pt_layer.loadNamedStyle(os.path.join(os.path.dirname(__file__), "styles", "mix_haisui_point.qml"))
                self.configure_mix_line_label(
                    page.line_layer,
                    self.clean_html_text(values.get("name")).strip(),
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
            self.log_calculation_detail(
                "測点の面積・外周長・延長の計算を完了しました。"
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
        compartment = self.location_forest_compartment()
        if not self.set_location_label_text(layout, compartment):
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
        backgrounds, styles = self.location_backgrounds(line_layers)
        map_item.setLayers(line_layers + backgrounds)
        map_item.setKeepLayerSet(True)
        if styles:
            map_item.setLayerStyleOverrides(styles)
            map_item.setKeepLayerStyles(True)

        extent = self.combined_layer_extent(line_layers)
        if extent is not None:
            map_item.zoomToExtent(extent)
            map_item.setScale(self.locationScale.scale())

        if not self.set_location_scale_bars(layout, map_item):
            return False

        self.htmlValues["rinshohan"] = self.rinshohan.text()
        safe_rinshohan = self.safe_file_name(compartment)
        output_path = (
            self.output_dir()
            / f"{safe_rinshohan} - 位置図.pdf"
        )
        if self._editable_projects is not None:
            self._editable_projects.capture(layout, "location", output_path.name,
                persistent_layers=self.editable_result_layers())
            return True
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

        if self.ichizuDirection.currentIndex() == 1:
            try:
                template_xml = self.landscape_location_template_xml(template_xml)
            except (ET.XMLSyntaxError, ValueError) as e:
                QMessageBox.warning(
                    self,
                    "エラー",
                    f"横向き位置図テンプレートを作成できません:\n{e}"
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

        layout = QgsPrintLayout(getattr(self, "render_project", QgsProject.instance()))
        layout.initializeDefaults()
        layout.loadFromTemplate(doc, QgsReadWriteContext())
        if self.ichizuDirection.currentIndex() == 1:
            map_item = layout.itemById("地図 1")
            if not isinstance(map_item, QgsLayoutItemMap):
                QMessageBox.warning(
                    self,
                    "エラー",
                    "位置図テンプレートに地図枠がありません"
                )
                return None
            # 地図項目はテンプレート読込時に既存範囲の縦横比へサイズを
            # 自動補正するため、読込後にA4横の枠へ確定させる。
            map_item.attemptResize(
                QgsLayoutSize(287, 200, QgsUnitTypes.LayoutMillimeters)
            )
            map_item.attemptMove(
                QgsLayoutPoint(148.5, 105, QgsUnitTypes.LayoutMillimeters),
                True,
            )
        if self.locationPaper.currentText() == "A3":
            self.resize_location_layout(layout, 2 ** 0.5)
        layout.setName("RingyoZumenMaker temporary location map")
        return layout

    def landscape_location_template_xml(self, template_xml):
        root = ET.fromstring(template_xml.encode("utf-8"))

        page_items = root.xpath("./PageCollection/LayoutItem[@type='65638']")
        if len(page_items) != 1:
            raise ValueError("位置図テンプレートの用紙が1枚ではありません")
        page_items[0].set("size", "297,210,mm")

        item_geometry = {
            # 地図枠は、縦向きの上下左右5mmの余白を横向きでも維持する。
            "地図 1": ("148.5,105,mm", "287,200,mm"),
            # 方位記号は右上へ移動する。
            "方位記号": ("261.863,9.67,mm", None),
            # 帯を構成する5要素は相対位置を保ったまま右下へ移動する。
            "縮尺": ("257.175,191.934,mm", None),
            "位置図見出し": ("92,191.934,mm", None),
            "スケールバー": ("289,200.068,mm", None),
            "位置図タイトル": ("169.7596,200.632,mm", None),
            "帯背景": ("291.91,204.91,mm", None),
        }
        for item_id, (position, size) in item_geometry.items():
            items = root.xpath(f"./LayoutItem[@id='{item_id}']")
            if len(items) != 1:
                raise ValueError(
                    f"位置図テンプレートの「{item_id}」が1つではありません"
                )
            item = items[0]
            item.set("position", position)
            item.set("positionOnPage", position)
            if size is not None:
                item.set("size", size)

        return ET.tostring(root, encoding="unicode")

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

    def location_forest_compartment(self):
        return self.clean_html_text(self.rinshohan.text()).strip()

    def set_location_label_text(self, layout, compartment):
        title_label = layout.itemById("位置図タイトル")
        if not isinstance(title_label, QgsLayoutItemLabel):
            QMessageBox.warning(
                self,
                "エラー",
                "位置図テンプレート内にタイトルラベルがありません"
            )
            return False

        parts = [
            compartment,
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

    def location_backgrounds(self, line_layers):
        project = QgsProject.instance()
        info = metadata(project) or {}
        managed = {layer_id for key, layer_id in info.get('datasets', {}).items() if not key.startswith('background:')}
        descriptor = next((entry for entry in info.get('layouts', []) if entry['key'] == 'location'), None)
        themes = project.mapThemeCollection()
        theme = ''
        if descriptor:
            layout = project.layoutManager().layoutByName(descriptor['layout'])
            map_item = self.first_layout_map_item(layout) if layout is not None else None
            theme = map_item.followVisibilityPresetName() if map_item and map_item.followVisibilityPreset() else descriptor['theme']
        if theme and themes.hasMapTheme(theme):
            candidates = themes.mapThemeVisibleLayers(theme)
            styles = themes.mapThemeStyleOverrides(theme)
        else:
            candidates = self.iface.mapCanvas().layers() if self.iface is not None else []
            styles = {}
        excluded_layers = set(line_layers + self.location_input_point_layers())
        backgrounds = [layer for layer in candidates
                       if layer.isValid() and layer not in excluded_layers and layer.id() not in managed]
        return backgrounds, {layer.id(): styles[layer.id()] for layer in backgrounds if layer.id() in styles}

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
        if self._editable_projects is None:
            return True
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
        layout = QgsPrintLayout(getattr(self, "render_project", QgsProject.instance()))
        layout.initializeDefaults()
        layout.setName("RingyoZumenMaker temporary 150x150 map")

        # ページ設定 150 x 150 mm
        page = layout.pageCollection().page(0)
        page.setPageSize(QgsLayoutSize(150 * self.paper_factor(), 150 * self.paper_factor(), QgsUnitTypes.LayoutMillimeters))

        # 地図アイテム作成
        map_item = QgsLayoutItemMap(layout)
        map_item.attemptMove(QgsLayoutPoint(0, 0, QgsUnitTypes.LayoutMillimeters))
        map_item.attemptResize(QgsLayoutSize(150 * self.paper_factor(), 150 * self.paper_factor(), QgsUnitTypes.LayoutMillimeters))
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
        validate_map_frame(map_item, target_layers)

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
        if self._editable_projects is not None:
            self._editable_projects.capture(layout, prefix, f"asset/{prefix}_map.png",
                persistent_layers=self.editable_result_layers())
            return True
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

        self._map_content_bounds[prefix] = rendered_map_bounds(
            output_path, 150 * self.paper_factor(), 150 * self.paper_factor(), prefix)
        return True

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


class RoleDrawingOperations:
    def location_forest_compartment(self):
        compartment = super().location_forest_compartment()
        if compartment:
            return compartment
        work_compartments = dict.fromkeys(
            value for drawing in self.role_drawings if drawing['role'] == 'work'
            for value in drawing['attributes']['forest_compartment']
        )
        return '、'.join(work_compartments)

    def apply_layer_color(self, layer, color, include_labels=False):
        # Keep every measurement label provider; a simple-label replacement would
        # discard the secondary measurement labels.
        super().apply_layer_color(layer, color, include_labels=False)
        if include_labels and layer is not None and layer.labeling() is not None:
            labeling = layer.labeling()
            for provider in labeling.subProviders() or ['']:
                settings = labeling.settings(provider)
                text_format = settings.format(); text_format.setColor(color); settings.setFormat(text_format)
                labeling.setSettings(settings, provider)

    def map_make(self, test=False):
        # The original point-connection, area and length calculations are shared.
        if not super().map_make(test=True): return False
        land = self.role_pages['land'] + self.role_pages['ex_polygon'] + self.role_pages['ex_line']
        self.role_drawings = []
        if self.role_pages['land']:
            self.role_drawings.append({'panel': 'land', 'title': '施行地・除地', 'pages': land,
                'attributes': self.attribute_values('land', self.role_pages['land']), 'role': 'land'})
        for index, page in enumerate(self.role_pages['work'], 1):
            self.role_drawings.append({'panel': 'work_' + page.drawing_uid, 'title': '付帯工作物 ' + (page.name_edit.text().strip() or str(index)),
                'pages': [page], 'attributes': self.attribute_values('work', [page]), 'role': 'work'})
        from .editable_projects import EditableProjects
        self._editable_projects = EditableProjects(self, self.output_dir(), getattr(self, '_project_snapshot', None)) if not test and self.outputMode.currentIndex() == 0 else None
        for drawing_index, drawing in enumerate(self.role_drawings, 1):
            layers = []
            for page in drawing['pages']:
                color = QColor('blue') if page.role.startswith('ex_') else QColor('black')
                for attr in ('pt_layer', 'line_layer'):
                    layer = getattr(page, attr)
                    self.apply_layer_color(layer, color, include_labels=True)
                    layers.append(layer)
                self.configure_mix_line_label(page.line_layer, page.name_edit.text().strip(), color)
            layout = self.create_layout(target_layers=layers)
            if layout is None: return False
            if not test and not self.export_layout_image(layout, drawing['panel']): return False
            self.progressBar.setValue(45 + round(30 * drawing_index / len(self.role_drawings)))
        if test: return True
        if self.isIchizu.isChecked():
            if not self.export_location_pdf(): return False
            self.progressBar.setValue(80)
        return self.save_result_layers_to_geopackage()

    def make_html(self, write_file=True):
        root = ET.parse(str(Path(__file__).parent / 'html_shinsoku/index.html'), ET.HTMLParser())
        root.xpath('//title')[0].text = ' - '.join(filter(None, (self.rinshohan.text().strip(), '実測図')))
        # Remove template placeholders; every detail belongs to a drawing.
        for node in root.xpath('//*[@data-option-panel="mix"]'):
            if node.get('id') != 'calc': node.getparent().remove(node)
        for key, _, element_id in FIELDS:
            container = root.xpath('//*[@id="' + element_id + '"]')[0]
            container.text = None
            for drawing in self.role_drawings:
                text = '、'.join(drawing['attributes'][key])
                node = ET.SubElement(container, 'span', **{'data-option-panel': drawing['panel'], 'title': text})
                node.text = text
        # Shared CRS; the map scale is recorded per diagram after regeneration.
        root.xpath('//*[@id="crs"]')[0].text = self.format_crs_display(self.crs.crs())
        scale_container = root.xpath('//*[@id="scale"]')[0]
        scale_container.text = None
        for drawing in self.role_drawings:
            panel = drawing['panel']
            self.update_zumen_options(root, panel, drawing['title'])
            rows = [row for page in drawing['pages'] for row in page.xy_table_rows]
            self.update_xy_tables(root, rows, panel)
            self.add_main_map(root, panel)
            value = ET.SubElement(scale_container, 'span', **{'data-option-panel': panel})
            value.text = '1/' + str(round(getattr(self, '_generated_scales', {}).get(panel, self.scale.scale())))
            if drawing['role'] == 'land':
                gross = sum(page.area for page in self.role_pages['land'])
                perimeter = sum(page.length for page in self.role_pages['land'])
                self.add_shui_detail(root, panel, perimeter, gross)
                with self.land_calculation_context():
                    self.add_calc_data(root, write_to_html=self.isJochikeisan.isChecked())
                root.xpath('//*[@id="calc"]')[0].set('data-option-panel', panel)
            else:
                page = drawing['pages'][0]
                self.add_haisui_length(root, panel, page.length)
                self.add_haisui_detail(root, panel, page.length, page.values()['haba'] if page.values()['has_haba'] else None)
                if page.name_edit.text().strip(): self.add_map_name(root, panel, page.name_edit.text().strip(), 'haisui')
        self.clean_html_tree(root)
        style = ET.SubElement(root.xpath('//head')[0], 'style')
        style.text = '@page { size: ' + self.paper.currentText() + ' landscape; margin: 0; }'
        root.xpath('//body')[0].set('data-paper', self.paper.currentText())
        root.xpath('//*[@id="main_container"]')[0].set('style', '--paper-factor:' + str(self.paper_factor()))
        if write_file:
            (self.output_dir() / 'index.html').write_text(ET.tostring(root, pretty_print=True, encoding='unicode', method='html'), encoding='utf-8')
            self.append_output_log('実測図を書き込みました: ' + str(self.displayed_output_path(self.output_dir() / 'index.html')))
        return True

    @contextmanager
    def land_calculation_context(self):
        old = self.shuis, self.haisuis, self.isShui.isChecked(), self.isHaisui.isChecked()
        try:
            self.shuis = self.role_pages['land'] + self.role_pages['ex_polygon']
            self.haisuis = self.role_pages['ex_line']
            self.isShui.setChecked(bool(self.shuis)); self.isHaisui.setChecked(bool(self.haisuis))
            yield
        finally:
            self.shuis, self.haisuis = old[:2]
            self.isShui.setChecked(old[2]); self.isHaisui.setChecked(old[3])
