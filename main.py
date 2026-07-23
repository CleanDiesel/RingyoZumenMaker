# -*- coding: utf-8 -*-

from qgis.PyQt.QtWidgets import (
    QWidget, QLabel, QLineEdit, QDoubleSpinBox, QFormLayout,
    QGroupBox, QGridLayout, QHBoxLayout,
    QRadioButton, QPushButton, QDockWidget, QMessageBox, QCheckBox,
    QScrollArea
)
from qgis.PyQt.QtCore import QDate
from qgis.PyQt.QtXml import QDomDocument
from qgis.gui import QgsMapLayerComboBox, QgsFieldExpressionWidget, QgsFieldComboBox
from qgis.core import (
    QgsWkbTypes, QgsMapLayerType, QgsVectorLayerSimpleLabeling, QgsMapLayerProxyModel, QgsProcessingModelAlgorithm,
    QgsProject, QgsPrintLayout, QgsLayoutItemMap, QgsLayoutPoint, QgsLayoutSize, QgsUnitTypes, QgsLayoutExporter,
    QgsPalLayerSettings, QgsExpression, QgsExpressionContext, QgsExpressionContextUtils, QgsLayoutItemLabel, QgsLayoutItemPicture,
    QgsSettings, QgsCoordinateReferenceSystem, QgsVectorFileWriter, QgsReadWriteContext,
)
from qgis.PyQt import uic
import os
from lxml import etree as ET
from pathlib import Path
import processing
from html import escape
import math
import shutil
import tempfile

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
        tab_scroll_area.setMinimumHeight(100)
        tab_scroll_area.setWidget(self.tabWidget)
        main_layout.insertWidget(0, tab_scroll_area, 1)

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

        self.setup_haisui_toolbox()
        self.restore_settings()

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

        self.save_settings()
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

            if not self.commit_staged_output(staging_dir, final_output_dir):
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

    def commit_staged_output(self, staging_dir, final_output_dir):
        staging_dir = Path(staging_dir)
        final_output_dir = Path(final_output_dir)
        backup_dir = staging_dir / ".backup"
        staged_files = [
            path
            for path in staging_dir.rglob("*")
            if path.is_file() and backup_dir not in path.parents
        ]
        committed = []

        try:
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

            QMessageBox.warning(
                self,
                "エラー",
                f"完成したファイルを出力先へ反映できません:\n{e}"
            )
            return False

        return True

    def open_output_tab(self):
        self.tabWidget.setCurrentWidget(self.tab_5)

    def get_shui_values(self):
        return {
            "point_layer": self.shui_point.currentLayer(),
            "filter_exp": self.shui_ex.expression(),
            "sokuten_label_exp": self.shui_sokutenLabel.expression(),
            "sort_exp": self.shui_sort.expression(),
        }

    def get_haisui_values(self):
        return [page.values() for page in self.haisuis]

    def settings_key(self, name):
        return f"zumen/{name}"

    def save_settings(self):
        settings = QgsSettings()

        for name in [
            "sokuryosha",
            "sokuryojigyosha",
            "rinshohan",
            "sanrinshoyusha",
            "seizujigyosha",
            "seizusha",
            "haisuiType",
        ]:
            settings.setValue(self.settings_key(name), getattr(self, name).text())

        settings.setValue(self.settings_key("sokuryobi"), self.sokuryobi.date().toString("yyyy-MM-dd"))
        settings.setValue(self.settings_key("seizubi"), self.seizubi.date().toString("yyyy-MM-dd"))
        settings.setValue(self.settings_key("scale"), self.scale.scale())
        settings.setValue(self.settings_key("crs"), self.crs.crs().authid())
        settings.setValue(self.settings_key("fileName"), self.fileName.filePath())
        settings.setValue(self.settings_key("ketasu"), self.ketasu.value())
        settings.setValue(self.settings_key("isJochikeisan"), self.isJochikeisan.isChecked())
        settings.setValue(self.settings_key("isIchizu"), self.isIchizu.isChecked())
        settings.setValue(self.settings_key("isShui"), self.isShui.isChecked())
        settings.setValue(self.settings_key("isHaisui"), self.isHaisui.isChecked())

    def restore_settings(self):
        settings = QgsSettings()

        for name in [
            "sokuryosha",
            "sokuryojigyosha",
            "rinshohan",
            "sanrinshoyusha",
            "seizujigyosha",
            "seizusha",
            "haisuiType",
        ]:
            value = settings.value(self.settings_key(name), "")
            if value:
                getattr(self, name).setText(str(value))

        self.restore_date_setting(settings, "sokuryobi", self.sokuryobi)
        self.restore_date_setting(settings, "seizubi", self.seizubi)

        scale_value = settings.value(self.settings_key("scale"), "")
        if scale_value not in ("", None):
            try:
                self.scale.setScale(float(scale_value))
            except (TypeError, ValueError):
                pass

        crs_value = settings.value(self.settings_key("crs"), "")
        if crs_value:
            crs = QgsCoordinateReferenceSystem(str(crs_value))
            if crs.isValid():
                self.crs.setCrs(crs)

        file_path = settings.value(self.settings_key("fileName"), "")
        if file_path:
            self.fileName.setFilePath(str(file_path))

        ketasu_value = settings.value(self.settings_key("ketasu"), "")
        if ketasu_value not in ("", None):
            try:
                self.ketasu.setValue(int(ketasu_value))
            except (TypeError, ValueError):
                pass

        self.isJochikeisan.setChecked(self.settings_bool(settings, "isJochikeisan", False))
        self.isIchizu.setChecked(self.settings_bool(settings, "isIchizu", False))
        self.isShui.setChecked(self.settings_bool(settings, "isShui", False))
        self.isHaisui.setChecked(self.settings_bool(settings, "isHaisui", False))

    def settings_bool(self, settings, name, default=False):
        value = settings.value(self.settings_key(name), default)
        if isinstance(value, bool):
            return value
        return str(value).lower() in ("1", "true", "yes")

    def restore_date_setting(self, settings, name, widget):
        value = settings.value(self.settings_key(name), "")
        for fmt in ("yyyy-MM-dd", "yyyy年MM月dd日"):
            date = QDate.fromString(str(value), fmt)
            if date.isValid():
                widget.setDate(date)
                return

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

        # 最初の1ページ
        self.add_haisui_page()

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
            "crs": self.crs.crs().authid(),
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
            self.update_zumen_options(root, "shui", "周囲")
            self.update_xy_tables(root, self.xy_table_rows, "shui")
            self.add_main_map(root, "shui")
        if self.isHaisui.isChecked():
            for page in self.haisuis:
                values = page.values()
                haisui_type = self.clean_html_text(self.haisuiType.text()).strip()
                name_text = self.clean_html_text(values.get("name")).strip()
                display_name = f"{haisui_type} {name_text}" if haisui_type else name_text
                self.update_zumen_options(root, f"haisui_{page.index}", display_name)
                self.update_xy_tables(root, page.xy_table_rows, f"haisui_{page.index}")
                self.add_main_map(root, f"haisui_{page.index}")
                self.add_haisui_length(root, f"haisui_{page.index}", page.length)
                self.add_haisui_detail(root, f"haisui_{page.index}", page.length, values.get("haba"))
                self.add_haisui_name(root, f"haisui_{page.index}", display_name)
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
        if write_to_html:
            calc.set("data-is-jochi-keisan", "1")

        items = []
        if self.isHaisui.isChecked():
            for page in self.haisuis:
                values = page.values()
                items.append({
                    "name": self.clean_html_text(values.get("name")),
                    "length": 0.0 if page.length is None else float(page.length),
                    "haba": float(values.get("haba") or 0.0),
                    "is_jochi": bool(values.get("is_jochi")),
                })

        type_text = self.clean_html_text(self.haisuiType.text()).strip()
        area_value = self.htmlValues.get("area", 0)
        all_distance = sum(item["length"] for item in items)
        jochi_items = [item for item in items if item["is_jochi"]]
        jochi_distance = sum(item["length"] for item in jochi_items)
        area_deduction = sum(self.haisui_calc_area(item) for item in jochi_items)
        area_diff = round(float(area_value or 0)) - round(area_deduction)

        all_terms = [
            f"{self.clean_html_text(item['name'])} {self.format_length(item['length'])}m"
            for item in items
        ]
        jochi_terms = [
            f"{self.clean_html_text(item['name'])} {self.format_length(item['length'])}m"
            for item in jochi_items
        ]
        area_terms = [
            self.haisui_area_term(item)
            for item in jochi_items
        ]
        all_distance_detail = f"{' + '.join(all_terms)} = {self.format_length(all_distance)}m"
        jochi_distance_detail = f"{' + '.join(jochi_terms)} = {self.format_length(jochi_distance)}m"
        area_deduction_detail = f"{' + '.join(area_terms)} = {self.format_area_int(area_deduction)}m2"
        area_diff_detail = (
            f"{self.format_area_int(area_value)}m2 - "
            f"{self.format_area_int(area_deduction)}m2 = "
            f"{self.format_area_int(area_diff)}m2 "
            f"≒ {self.format_area_ha(area_diff)}ha"
        )

        self.append_output_log(f"周囲外周長: {self.format_length(self.htmlValues.get('shui_length', 0))}m")
        self.append_output_log(f"周囲面積: {self.format_area_int(area_value)}m2")
        self.append_output_log(f"{type_text}延長: {all_distance_detail}")
        self.append_output_log(f"除地延長: {jochi_distance_detail}")
        self.append_output_log(f"除地面積: {area_deduction_detail}")
        self.append_output_log(f"差引面積: {area_diff_detail}")

        if write_to_html:
            self.set_calc_distance(
                root,
                "calc_distance_haisuikou",
                f"{type_text}：",
                all_distance_detail,
            )
            self.set_calc_distance(
                root,
                "calc_distance_jochi",
                "除地：",
                jochi_distance_detail,
            )
            self.set_calc_text(root, "box1", f"{type_text}：")
            self.set_calc_text(root, "box2", f"{self.format_length(all_distance)}m")
            self.set_calc_area(root, "box4", f"{' + '.join(area_terms)} = {self.format_area_int(area_deduction)}m")
            self.set_calc_area(root, "box5", f"≒ {self.format_area_int(area_deduction)}m")
            self.set_calc_area_diff(root, "box7", area_value, area_deduction)
            self.set_calc_area_with_ha(root, "box8", area_diff)

    def format_length(self, value):
        return f"{float(value or 0):.1f}"

    def format_area_int(self, value):
        return str(round(float(value or 0)))

    def format_area_ha(self, value):
        return f"{math.trunc((float(value or 0) / 10000) * 100) / 100:.2f}"

    def haisui_calc_area(self, item):
        area = float(item.get("length") or 0) * float(item.get("haba") or 0)
        return 0 if area < 100 else area

    def haisui_area_term(self, item):
        name = self.clean_html_text(item["name"])
        raw_area = float(item.get("length") or 0) * float(item.get("haba") or 0)
        term = f"{name} {self.format_length(item['length'])}*{self.format_length(item['haba'])}"
        if 0 < raw_area < 100:
            return f"{term}(0m2)"
        return term

    def replace_children_with_text(self, elem, text):
        elem.text = text
        for child in list(elem):
            elem.remove(child)

    def set_calc_text(self, root, elem_id, text):
        elems = root.xpath(f"//*[@id='{elem_id}']")
        if elems:
            self.replace_children_with_text(elems[0], text)

    def set_calc_distance(self, root, elem_id, label, detail):
        elems = root.xpath(f"//*[@id='{elem_id}']")
        if not elems:
            return

        elem = elems[0]
        self.replace_children_with_text(elem, label)
        p = ET.SubElement(elem, "p")
        p.text = detail

    def set_calc_area(self, root, elem_id, text_before_sup):
        elems = root.xpath(f"//*[@id='{elem_id}']")
        if not elems:
            return

        elem = elems[0]
        self.replace_children_with_text(elem, text_before_sup)
        sup = ET.SubElement(elem, "sup")
        sup.text = "2"

    def set_calc_area_diff(self, root, elem_id, area_value, area_deduction):
        elems = root.xpath(f"//*[@id='{elem_id}']")
        if not elems:
            return

        elem = elems[0]
        self.replace_children_with_text(elem, f"{self.format_area_int(area_value)}m")
        first_sup = ET.SubElement(elem, "sup")
        first_sup.text = "2"
        first_sup.tail = f" - {self.format_area_int(area_deduction)}m"
        second_sup = ET.SubElement(elem, "sup")
        second_sup.text = "2"

    def set_calc_area_with_ha(self, root, elem_id, area_value):
        elems = root.xpath(f"//*[@id='{elem_id}']")
        if not elems:
            return

        elem = elems[0]
        self.replace_children_with_text(elem, f"= {self.format_area_int(area_value)}m")
        sup = ET.SubElement(elem, "sup")
        sup.text = "2"
        sup.tail = f" ≒ {self.format_area_ha(area_value)}ha"

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

        chunks = [
            xy_table_rows[i:i + 25]
            for i in range(0, len(xy_table_rows), 25)
        ]

        for index, rows in enumerate(chunks, start=1):
            table = self.create_xy_table(option_panel, index, rows)
            table.set("data-xy-table", str(index))
            if index != 1:
                table.set("hidden", "hidden")
            xy_container.append(table)

        self.show_xy_updown(root, option_panel, len(chunks))

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
        rows_root = ET.fromstring(f"<tbody>{''.join(rows)}</tbody>")
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
        self.add_haisui_detail_item(
            detail,
            f"detail_haba_{option_panel}",
            option_panel,
            "排水幅",
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
        span.tail = "m"

        before = detail.xpath(before_xpath)
        if before:
            detail.insert(detail.index(before[0]), div)
        else:
            detail.append(div)

    def add_haisui_name(self, root, option_panel, name):
        locations = root.xpath("//*[@id='location']/div[contains(concat(' ', normalize-space(@class), ' '), ' value ')]")
        if not locations:
            return

        item_id = f"haisui_name_{option_panel}"
        if root.xpath(f"//*[@id='{item_id}']"):
            return

        div = ET.Element(
            "div",
            id=item_id,
            **{
                "class": "value3",
                "data-option-panel": f"haisui {option_panel}",
            }
        )
        div.text = "" if name is None else str(name)
        locations[0].append(div)

    def validate_inputs(self):
        if (
            self.isShui.isChecked()
        ):
            layer = self.shui_point.currentLayer()
            if layer is None:
                QMessageBox.warning(
                    self,
                    "エラー",
                    "レイヤを選択してください"
                )
                return

            if layer.type() != QgsMapLayerType.VectorLayer or QgsWkbTypes.geometryType(
                layer.wkbType()
            ) != QgsWkbTypes.PointGeometry:
                QMessageBox.warning(
                    self,
                    "エラー",
                    "ポイントレイヤのみ使用できます"
                )
                return
            
            if not self.shui_sokutenLabel.expression().strip():
                QMessageBox.warning(
                    self,
                    "エラー",
                    "測点名を選択してください"
                )
                return
        if (
            self.isHaisui.isChecked()
        ):
            if not self.haisuis:
                QMessageBox.warning(
                    self,
                    "エラー",
                    "少なくとも1つの排水・林内路網を追加してください"
                )
                return

            # 各排水・林内路網ページについて、ポイントレイヤと属性指定のバリデーションを行う
            for page in self.haisuis:
                layer = page.point_layer.currentLayer()
                if not page.name_edit.text().strip():
                    QMessageBox.warning(
                        self,
                        "エラー",
                        f"排水・林内路網 {page.index}: 名前を入力してください"
                    )
                    return

                if layer is None:
                    QMessageBox.warning(
                        self,
                        "エラー",
                        f"排水・林内路網 {page.index}: レイヤを選択してください"
                    )
                    return

                if layer.type() != QgsMapLayerType.VectorLayer or QgsWkbTypes.geometryType(
                    layer.wkbType()
                ) != QgsWkbTypes.PointGeometry:
                    QMessageBox.warning(
                        self,
                        "エラー",
                        f"排水・林内路網 {page.index}: ポイントレイヤのみ使用できます"
                    )
                    return

                if not page.sokuten_label_exp.expression().strip():
                    QMessageBox.warning(
                        self,
                        "エラー",
                        f"排水・林内路網 {page.index}: 測点名を選択してください"
                    )
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
    
    def map_make(self, test=False):
        if self.isShui.isChecked():
            make_layer = self.load_model("make_layer.model3")

            params = {
                'crs': self.crs.crs(),
                'gpx': self.get_shui_values().get('point_layer'),
                'gpx_extract': self.get_shui_values().get('filter_exp') or True,
                'order': self.get_shui_values().get('sort_exp'),
                'shui_pt': 'memory:',
                'shui': 'memory:',
                'area': 'memory:'
            }

            try:
                result = processing.run(make_layer, params)
                self.LayerSet['shui'] = {
                    'pt': result['shui_pt'],
                    'line': result['shui'],
                }
                point_count = self.LayerSet['shui']['pt'].featureCount()
                if point_count < 3:
                    raise ValueError(
                        f"抽出後の周囲ポイントは{point_count}点です。"
                        "周囲図の作成には3点以上必要です"
                    )
                shui_area_layer = result['area']
                self.LayerSet['shui']['pt'].loadNamedStyle(os.path.join(os.path.dirname(__file__), "styles", "point.qml"))
                s = self.LayerSet['shui']['pt'].labeling().settings()
                s.fieldName = self.get_shui_values().get('sokuten_label_exp')
                s.isExpression = True
                self.LayerSet['shui']['pt'].setLabeling(QgsVectorLayerSimpleLabeling(s))
                self.LayerSet['shui']['line'].loadNamedStyle(os.path.join(os.path.dirname(__file__), "styles", "line.qml"))
                self.htmlValues['shui_length'] = round(sum(f.geometry().length() for f in self.LayerSet['shui']['line'].getFeatures()), 1)
                self.htmlValues['area'] = int(sum(f.geometry().area() for f in shui_area_layer.getFeatures()))
                self.htmlValues['area2'] = self.htmlValues['area']
                self.htmlValues['area_ha'] = self.format_area_ha(self.htmlValues['area'])
                self.xy_table_rows = self.point_layer_to_html_rows(
                    self.LayerSet['shui']['pt'],
                    name_expression=s.fieldName,
                )
            except Exception as e:
                QMessageBox.warning(
                    self,
                    "エラー",
                    f"周囲図の作成に失敗しました:\n{e}"
                )
                return False

            if not test:
                layout = self.create_layout(
                    target_layers=[
                        self.LayerSet['shui']['pt'],
                        self.LayerSet['shui']['line'],
                    ]
                )
                if layout is None:
                    return False
                if not self.export_layout_image(layout, "shui"):
                    return False
        
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
                    point_count = page.pt_layer.featureCount()
                    if point_count < 2:
                        raise ValueError(
                            f"抽出後のポイントは{point_count}点です。"
                            "線の作成には2点以上必要です"
                        )
                    page.pt_layer.loadNamedStyle(os.path.join(os.path.dirname(__file__), "styles", "point.qml"))
                    s = page.pt_layer.labeling().settings()
                    s.fieldName = values.get('sokuten_label_exp')
                    s.isExpression = True
                    page.pt_layer.setLabeling(QgsVectorLayerSimpleLabeling(s))
                    page.line_layer.loadNamedStyle(os.path.join(os.path.dirname(__file__), "styles", "line.qml"))
                    length = round(sum(f.geometry().length() for f in page.line_layer.getFeatures()), 1)
                    page.length = length
                    page.xy_table_rows = self.point_layer_to_html_rows(
                        page.pt_layer,
                        name_expression=s.fieldName,
                    )
                except Exception as e:
                    QMessageBox.warning(
                        self,
                        "エラー",
                        f"排水・林内路網 {page.index} の作成に失敗しました:\n{e}"
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
            target_layers = []
            if self.isShui.isChecked():
                target_layers.extend([self.LayerSet['shui']['pt'], self.LayerSet['shui']['line']])

            if self.isHaisui.isChecked():
                for page in self.haisuis:
                    if page.pt_layer is None or page.line_layer is None:
                        continue
                    page.pt_layer.loadNamedStyle(os.path.join(os.path.dirname(__file__), "styles", "mix_haisui_point.qml"))
                    page.line_layer.loadNamedStyle(os.path.join(os.path.dirname(__file__), "styles", "mix_haisui_line.qml"))
                    labeling = page.line_layer.labeling()
                    s = labeling.settings() if labeling else QgsPalLayerSettings()
                    label_text = self.clean_html_text(page.values().get("name")).replace("'", "''")
                    s.fieldName = f"'{label_text}'"
                    s.isExpression = True
                    page.line_layer.setLabeling(QgsVectorLayerSimpleLabeling(s))
                    page.line_layer.setLabelsEnabled(True)
                    target_layers.extend([page.pt_layer, page.line_layer])

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
            self.LayerSet["shui"]["line"].loadNamedStyle(str(style_dir / "location_shui.qml"))

        if self.isHaisui.isChecked():
            for page in self.haisuis:
                if page.line_layer is not None:
                    page.line_layer.loadNamedStyle(str(style_dir / "location_haisui.qml"))

        template_path = style_dir / "location.qpt"
        layout = self.create_location_layout_from_template(template_path)
        if layout is None:
            return False

        self.set_location_picture_paths(layout, style_dir)
        self.set_location_label_text(layout)

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

        extent = self.combined_layer_extent(line_layers)
        if extent is not None:
            map_item.zoomToExtent(extent)
            map_item.setScale(self.scale.scale())

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
        return True

    def location_line_layers(self):
        layers = []
        if self.isShui.isChecked():
            layers.append(self.LayerSet["shui"]["line"])

        if self.isHaisui.isChecked():
            for page in self.haisuis:
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

        layout_name = "location_map"

        layout = QgsPrintLayout(QgsProject.instance())
        layout.initializeDefaults()
        layout.loadFromTemplate(doc, QgsReadWriteContext())
        layout.setName(layout_name)
        return layout

    def set_location_picture_paths(self, layout, style_dir):
        north_arrow = layout.itemById("方位記号")
        if isinstance(north_arrow, QgsLayoutItemPicture):
            north_arrow.setPicturePath(str(style_dir / "houi2.svg"))

    def set_location_label_text(self, layout):
        text = "\n".join([
            self.clean_html_text(self.sanrinshoyusha.text()),
            self.clean_html_text(self.rinshohan.text()),
        ]).strip()

        for item in layout.items():
            if isinstance(item, QgsLayoutItemLabel) and not self.clean_html_text(item.text()).strip():
                item.setText(text)
                item.adjustSizeToText()
                return

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
            layer = self.get_shui_values().get("point_layer")
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
        output_dir = self.output_dir()
        gpkg_path = output_dir / "ringyo_zumen.gpkg"

        layers = []
        if self.isShui.isChecked():
            layers.extend([
                {
                    "layer": self.LayerSet["shui"]["pt"],
                    "name": "周囲_点",
                    "style": "point.qml",
                },
                {
                    "layer": self.LayerSet["shui"]["line"],
                    "name": "周囲_ライン",
                    "style": "line.qml",
                },
            ])

        if self.isHaisui.isChecked():
            for page in self.haisuis:
                if page.pt_layer is None or page.line_layer is None:
                    continue

                name = self.safe_gpkg_layer_name(page.values().get("name") or page.index)
                layers.extend([
                    {
                        "layer": page.pt_layer,
                        "name": f"排水_{page.index}_{name}_点",
                        "style": "mix_haisui_point.qml",
                    },
                    {
                        "layer": page.line_layer,
                        "name": f"排水_{page.index}_{name}_ライン",
                        "style": "mix_haisui_line.qml",
                    },
                ])

        if not layers:
            return True

        for i, item in enumerate(layers):
            options = QgsVectorFileWriter.SaveVectorOptions()
            options.driverName = "GPKG"
            options.layerName = item["name"]
            options.fileEncoding = "UTF-8"
            options.actionOnExistingFile = (
                QgsVectorFileWriter.CreateOrOverwriteFile
                if i == 0
                else QgsVectorFileWriter.CreateOrOverwriteLayer
            )

            result = QgsVectorFileWriter.writeAsVectorFormatV3(
                item["layer"],
                str(gpkg_path),
                QgsProject.instance().transformContext(),
                options,
            )
            error_code = result[0]
            error_message = result[1] if len(result) > 1 else ""

            if error_code != QgsVectorFileWriter.NoError:
                QMessageBox.warning(
                    self,
                    "エラー",
                    f"GeoPackageへの保存に失敗しました:\n{item['name']}\n{error_message}"
                )
                return False

        self.append_output_log(
            f"GeoPackageを書き込みました: {self.displayed_output_path(gpkg_path)}"
        )
        return True

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
            return self.clean_html_text(feature["name"]).strip()

        expression = QgsExpression(name_expression)
        if expression.hasParserError():
            raise ValueError(f"測点名の式が正しくありません: {expression.parserErrorString()}")

        context = QgsExpressionContext()
        context.appendScopes(QgsExpressionContextUtils.globalProjectLayerScopes(layer))
        context.setFeature(feature)

        value = expression.evaluate(context)
        if expression.hasEvalError():
            raise ValueError(f"測点名の評価に失敗しました: {expression.evalErrorString()}")

        return self.clean_html_text(value).strip()

    def point_layer_to_html_rows(self, layer, name_prefix=None, name_expression=None):
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
                prefix = self.clean_html_text(name_prefix).strip()
                if prefix and raw_name and not raw_name.startswith(prefix):
                    raw_name = f"{prefix}{raw_name}"
                elif prefix and not raw_name:
                    raw_name = prefix
                name = escape(raw_name)
                x = float(f["x"])
                y = float(f["y"])
            except (KeyError, TypeError, ValueError) as e:
                raise ValueError(f"座標表の作成に失敗しました feature id={f.id()}") from e

            rows.append(
                f"<tr><td>{name}</td><td>{self.format_coordinate(x)}</td><td>{self.format_coordinate(y)}</td></tr>"
            )

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
        layout_name = "150x150_map"

        layout = QgsPrintLayout(QgsProject.instance())
        layout.initializeDefaults()
        layout.setName(layout_name)

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

        result = exporter.exportToImage(output_path, settings)
        if result != QgsLayoutExporter.Success:
            QMessageBox.warning(
                self,
                "エラー",
                f"地図画像の保存に失敗しました: "
                f"{self.displayed_output_path(output_path)}"
            )
            return False

        return True

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

        # 幅
        self.haba_spin = QDoubleSpinBox()
        self.haba_spin.setValue(1.0)
        self.haba_spin.setDecimals(2)
        self.haba_spin.setSuffix(" m")
        layout.addRow("幅（m）", self.haba_spin)

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

        self.sokuten_label_exp = QgsFieldExpressionWidget()
        self.sort_exp = QgsFieldExpressionWidget()

        self.sokuten_label_exp.setLayer(self.point_layer.currentLayer())
        self.sort_exp.setLayer(self.point_layer.currentLayer())

        self.point_layer.layerChanged.connect(self.sokuten_label_exp.setLayer)
        self.point_layer.layerChanged.connect(self.sort_exp.setLayer)

        attr_layout.addWidget(QLabel("測点名指定"), 0, 0)
        attr_layout.addWidget(self.sokuten_label_exp, 0, 1)
        attr_layout.addWidget(QLabel("結合順指定"), 1, 0)
        attr_layout.addWidget(self.sort_exp, 1, 1)

        layout.addRow(attr_group)

        # 削除ボタン
        self.delete_button = QPushButton("削除")
        layout.addRow(self.delete_button)

    def values(self):
        return {
            "name": self.name_edit.text(),
            "haba": self.haba_spin.value(),
            "is_jochi": self.is_jochi.isChecked(),
            "point_layer": self.point_layer.currentLayer(),
            "filter_exp": self.filter_exp.expression(),
            "sokuten_label_exp": self.sokuten_label_exp.expression(),
            "sort_exp": self.sort_exp.expression(),
        }
