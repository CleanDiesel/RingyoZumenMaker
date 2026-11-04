"""Point-based survey drawings grouped as land, exclusions and ancillary works."""
import copy
from pathlib import Path
from contextlib import contextmanager

from lxml import etree as ET
from qgis.PyQt.QtCore import QDate
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox, QTabWidget,
    QPushButton, QLabel, QLineEdit, QDateEdit, QMessageBox,
)
from qgis.core import QgsProperty


from .drawing_inputs import FIELDS
from .drawing_core import RoleDrawingOperations


class InputTabs(QTabWidget):
    """Named input groups without compressing collapsed pages into the form."""
    def addItem(self, page, label):
        return self.addTab(page, label)

    def setItemText(self, index, label):
        self.setTabText(index, label)

    def removeItem(self, index):
        self.removeTab(index)


class MeasurementWorkflow(RoleDrawingOperations):

    def setup_output_options(self):
        super().setup_output_options()
        self.role_pages = {role: [] for role in ('land', 'ex_polygon', 'ex_line', 'work')}
        self.role_boxes = {}
        self.attribute_inputs = {'land': {}}
        # Keep the original input objects for v1/v2 config migration and geometry
        # helpers, but use the new role-specific tabs for all user interaction.
        self.groupBox_5.hide()
        basic = self.tabWidget.widget(0)
        output = self.tab_5
        for index in range(self.tabWidget.count() - 1, -1, -1):
            widget = self.tabWidget.widget(index)
            if widget not in (basic, output):
                self.tabWidget.removeTab(index)
                widget.hide()
        self.organize_basic_tab(basic)
        self.land_tab = self.role_tab('施行地', [('land', '施行地ポリゴン')])
        self.ex_tab = self.role_tab('除地', [('ex_polygon', '除地ポリゴン'), ('ex_line', '除地ライン')])
        self.work_tab = self.role_tab('付帯工作物', [('work', '付帯工作物ライン')])
        self.add_attributes(self.land_tab.layout(), 'land')
        # Output remains the final tab.
        self.tabWidget.removeTab(self.tabWidget.indexOf(output))
        self.tabWidget.addTab(output, '出力')
        self.tabWidget.setCurrentIndex(0)
        self._role_ui_ready = True
        self.setup_processing_actions()

    def setup_processing_actions(self):
        from qgis.PyQt.QtWidgets import QMenu, QApplication
        from qgis.core import QgsApplication, QgsProcessingContext, QgsProject
        from .processing_provider import DrawingProvider, ALGORITHM_ID, parameters_from_dock
        import json

        button = QPushButton('プロセシング')
        menu = QMenu(button)
        button.setMenu(menu)
        self.processing_button = button
        self._content_widget.layout().itemAt(2).layout().insertWidget(2, button)

        def algorithm():
            registry = QgsApplication.processingRegistry()
            if registry.providerById('ringyozumenmaker') is None:
                registry.addProvider(DrawingProvider())
            return registry.algorithmById(ALGORITHM_ID)

        def show_dialog():
            import processing
            algorithm()
            processing.execAlgorithmDialog(ALGORITHM_ID, parameters_from_dock(self))

        def show_batch():
            from processing.gui.BatchAlgorithmDialog import BatchAlgorithmDialog
            dialog = BatchAlgorithmDialog(algorithm().create(), self.window())
            dialog.exec()

        def copy_python():
            context = QgsProcessingContext()
            context.setProject(QgsProject.instance())
            command = algorithm().asPythonCommand(parameters_from_dock(self), context)
            QApplication.clipboard().setText(command)
            self.append_output_log('Pythonコマンドをコピーしました。')

        def copy_json():
            algorithm()
            value = {'algorithm_id': ALGORITHM_ID, 'parameters': parameters_from_dock(self)}
            QApplication.clipboard().setText(json.dumps(value, ensure_ascii=False, indent=2))
            self.append_output_log('実行設定をJSONとしてコピーしました。')

        for label, callback in (('プロセシングで開く…', show_dialog), ('バッチ処理…', show_batch),
                                ('Pythonコマンドをコピー', copy_python), ('JSONとしてコピー', copy_json)):
            action = menu.addAction(label)
            action.triggered.connect(callback)

    def role_tab(self, title, roles):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        explanation = '測点を結ぶ順序と測点名を指定します。施行地と除地は一つの実測図にまとめ、付帯工作物は登録ごとに一つの実測図を作成します。'
        hint = QLabel(explanation); hint.setWordWrap(True); layout.addWidget(hint)
        for role, label in roles:
            group = QGroupBox(label)
            column = QVBoxLayout(group)
            button = QPushButton(label + 'を追加')
            button.clicked.connect(lambda checked=False, role=role: self.add_role_page(role))
            column.addWidget(button)
            box = InputTabs(); box.setMinimumHeight(450)
            column.addWidget(box)
            self.role_boxes[role] = box
            layout.addWidget(group)
        self.tabWidget.addTab(tab, title)
        return tab

    def add_role_page(self, role, item=None):
        from .main import ShuiPage, HaisuiPage
        pages, box = self.role_pages[role], self.role_boxes[role]
        page = ShuiPage(len(pages) + 1, self) if role in ('land', 'ex_polygon') else HaisuiPage(len(pages) + 1, self, required_width=role == 'ex_line')
        page.role = role
        page.name_edit.setText({'land': '施行地', 'ex_polygon': '除地', 'ex_line': '除地ライン', 'work': '付帯工作物'}[role] + str(len(pages) + 1))
        page.is_jochi.setChecked(role.startswith('ex_'))
        page.is_jochi.hide()
        label = page.layout().labelForField(page.is_jochi)
        if label: label.hide()
        if hasattr(page, 'haba_spin'):
            page.haba_spin.setMinimum(.01); page.haba_spin.setMaximum(100000)
        page.delete_button.clicked.connect(lambda checked=False, page=page: self.remove_role_page(page))
        pages.append(page)
        box.addItem(page, str(len(pages)))
        page.name_edit.textChanged.connect(lambda text, page=page: self.update_page_title(page))
        box.setCurrentIndex(len(pages) - 1)
        if item:
            page.drawing_uid = item.get('drawing_uid') or page.drawing_uid
            page.name_edit.setText(str(item.get('name', '')))
            page.point_layer.setLayer(self.resolve_layer_reference(item.get('layer')))
            page.filter_exp.setExpression(str(item.get('filter_expression', '')))
            page.sort_exp.setExpression(str(item.get('sort_expression', '')))
            page.sokuten_labels.set_expressions(item.get('point_name_expressions'), item.get('primary_point_name_index', 0),
                fallback_expression=str(item.get('point_name_expression', '')))
            if hasattr(page, 'haba_spin'):
                page.type_edit.setText(str(item.get('type', '排水')))
                if page.isHaba is not None:
                    page.isHaba.setChecked(bool(item.get('has_width', True)))
                page.haba_spin.setValue(float(item.get('width', 1)))
        if role == 'work':
            page.attribute_inputs = self.make_attribute_fields(page.layout())
            if item and 'drawing_attributes' in item:
                self.restore_attribute_fields(page.attribute_inputs, item['drawing_attributes'])
        self.sync_role_pages()
        self.update_page_title(page)
        return page

    def update_page_title(self, page):
        box = self.role_boxes[page.role]
        index = box.indexOf(page)
        if index >= 0: box.setItemText(index, page.name_edit.text().strip() or str(index + 1))

    def remove_role_page(self, page):
        pages, box = self.role_pages[page.role], self.role_boxes[page.role]
        page.hide(); box.removeItem(box.indexOf(page)); pages.remove(page); page.deleteLater()
        for index, item in enumerate(pages):
            item.index = index + 1
            box.setItemText(index, str(index + 1))
        self.sync_role_pages()

    def sync_role_pages(self):
        self.shuis = self.role_pages['land'] + self.role_pages['ex_polygon']
        self.haisuis = self.role_pages['ex_line'] + self.role_pages['work']
        for index, page in enumerate(self.shuis, 1): page.index = index
        for index, page in enumerate(self.haisuis, 1): page.index = index
        self.isShui.setChecked(bool(self.shuis))
        self.isHaisui.setChecked(bool(self.haisuis))

    def organize_basic_tab(self, previous):
        basic = QWidget()
        column = QVBoxLayout(basic)
        self.basic_groups = {}
        groups = (
            ('座標系', [('CRS', self.crs)]),
            ('地図', [('図面の用紙サイズ', self.paper), ('縮尺', self.scale)]),
            ('位置図', [('位置図の生成', self.isIchizu), ('用紙サイズ', self.locationPaper),
                ('向き', self.ichizuDirection), ('縮尺', self.locationScale)]),
            ('計算・表示', [('施行地・除地の図面に計算を表示', self.isJochikeisan),
                ('最小除地面積', self.minEx), ('座標表示の小数桁数', self.ketasu),
                ('座標表の測点並べ替え', self.hyouOrder), ('延長表示の小数桁数', self.lengthDecimals),
                ('面積（m²）表示の小数桁数', self.areaDecimals), ('面積（ha）表示の小数桁数', self.haDecimals)]),
        )
        for title, rows in groups:
            group = QGroupBox(title)
            form = QFormLayout(group)
            for label, widget in rows:
                form.addRow(label, widget)
            column.addWidget(group)
            self.basic_groups[title] = group
        column.addStretch()
        index = self.tabWidget.indexOf(previous)
        self.tabWidget.removeTab(index)
        previous.hide()
        self.tabWidget.insertTab(index, basic, '基本情報')
        self.basic_tab = basic

    def make_attribute_fields(self, layout, use_land_fields=False):
        group = QGroupBox('属性・測量情報（この図面）')
        form = QFormLayout(group)
        fields = {}
        for key, label, original_name in FIELDS:
            if use_land_fields:
                widget = getattr(self, original_name)
            else:
                widget = QDateEdit() if key.endswith('_date') else QLineEdit()
            if isinstance(widget, QDateEdit):
                widget.setDisplayFormat('yyyy年MM月dd日')
                widget.setCalendarPopup(True)
                widget.setDate(QDate.currentDate())
            form.addRow(label, widget)
            fields[key] = widget
        if isinstance(layout, QFormLayout):
            layout.insertRow(max(0, layout.rowCount() - 1), group)
        else:
            layout.addWidget(group)
        return fields

    def add_attributes(self, layout, role):
        self.attribute_inputs[role] = self.make_attribute_fields(layout, use_land_fields=True)

    @staticmethod
    def serialize_attribute_fields(fields):
        return {key: widget.date().toString('yyyy-MM-dd') if isinstance(widget, QDateEdit) else widget.text()
                for key, widget in fields.items()}

    @staticmethod
    def restore_attribute_fields(fields, data):
        for key, widget in fields.items():
            if key not in data:
                continue
            value = data[key]
            # Earlier configs paired literal inputs with a QGIS property. Only
            # the saved literal is used; point attributes are never evaluated.
            if isinstance(value, dict):
                value = value.get('value', '')
            if isinstance(widget, QDateEdit):
                date = QDate.fromString(str(value), 'yyyy-MM-dd')
                if not date.isValid():
                    raise ValueError('日付の指定を確認してください: ' + key)
                widget.setDate(date)
            else:
                widget.setText('' if value is None else str(value))

    def configuration_data(self):
        data = super().configuration_data()
        if not getattr(self, '_role_ui_ready', False): return data
        data['workflow'] = 'roles'
        for group, pages in (('polygons', self.shuis), ('lines', self.haisuis)):
            for item, page in zip(data[group]['items'], pages): item['role'] = page.role
        data['attribute_input_mode'] = 'literal_per_drawing'
        data['drawing_attributes'] = {'land': self.serialize_attribute_fields(self.attribute_inputs['land'])}
        for item, page in zip(data['lines']['items'], self.haisuis):
            if page.role == 'work':
                item['drawing_attributes'] = self.serialize_attribute_fields(page.attribute_inputs)
        from .unified_project import source_key, feature_uid
        data['selected_inputs'] = {}
        for page in self.shuis + self.haisuis:
            layer = page.point_layer.currentLayer()
            if layer:
                selected = set(layer.selectedFeatureIds())
                data['selected_inputs'][source_key(layer)] = [feature_uid(layer, f) for f in layer.getFeatures() if f.id() in selected]
        return data

    def apply_configuration(self, data):
        if not getattr(self, '_role_ui_ready', False): return super().apply_configuration(data)
        # Restore common settings using the original reader without making old
        # toolbox pages; then rebuild the role-specific inputs.
        settings = copy.deepcopy(data)
        settings['polygons'] = {'items': [], 'enabled': False}
        settings['lines'] = {'items': [], 'enabled': False}
        super().apply_configuration(settings)
        for role, box in self.role_boxes.items():
            while box.count():
                page = box.widget(0); page.hide(); box.removeItem(0); page.deleteLater()
            self.role_pages[role] = []
        missing = []
        for group, default in (('polygons', 'land'), ('lines', 'work')):
            source = data.get(group, data.get('perimeters' if group == 'polygons' else 'drainage', {}))
            for item in source.get('items', []):
                role = item.get('role') or (('ex_polygon' if group == 'polygons' else 'ex_line') if item.get('deduct_area') else default)
                if role not in self.role_pages: raise ValueError('不明な入力区分です: ' + str(role))
                page = self.add_role_page(role, item)
                if item.get('layer') and page.point_layer.currentLayer() is None:
                    missing.append(str(item['layer'].get('name', role)))
        attributes = data.get('drawing_attributes', {})
        self.restore_attribute_fields(self.attribute_inputs['land'], attributes.get('land', {}))
        if data.get('attribute_input_mode') != 'literal_per_drawing':
            # Duplicate the former shared literals into each drawing, preserving
            # explicit former inheritance as a one-time copy during migration.
            shared = attributes.get('work', {})
            land = self.serialize_attribute_fields(self.attribute_inputs['land'])
            for page in self.role_pages['work']:
                migrated = {}
                for key, value in shared.items():
                    prop = QgsProperty()
                    if isinstance(value, dict):
                        prop.loadVariant(value.get('property', {}))
                    inherited = prop.isActive() and prop.asExpression() == '@rz_land_' + key
                    migrated[key] = land.get(key, '') if inherited else value
                if not shared:
                    migrated = land
                self.restore_attribute_fields(page.attribute_inputs, migrated)
        self.sync_role_pages()
        from .unified_project import source_key, feature_uid
        for page in self.shuis + self.haisuis:
            layer = page.point_layer.currentLayer()
            if layer and source_key(layer) in data.get('selected_inputs', {}):
                selected = set(data['selected_inputs'][source_key(layer)])
                layer.selectByIds([f.id() for f in layer.getFeatures() if feature_uid(layer, f) in selected])
        return missing

    def validate_inputs(self):
        self.sync_role_pages()
        if (self.role_pages['ex_polygon'] or self.role_pages['ex_line']) and not self.role_pages['land']:
            QMessageBox.warning(self, 'エラー', '除地を指定する場合は施行地も指定してください')
            return False
        return super().validate_inputs()

    def attribute_values(self, role, pages):
        fields = self.attribute_inputs['land'] if role == 'land' else pages[0].attribute_inputs
        values = {}
        for key, widget in fields.items():
            if isinstance(widget, QDateEdit):
                date = widget.date()
                if not date.isValid():
                    raise ValueError('日付の指定を確認してください: ' + key)
                text = date.toString('yyyy年MM月dd日')
            else:
                text = widget.text().strip()
            values[key] = [text] if text else []
        if not values['drawing_company']:
            values['drawing_company'] = values['survey_company']
        if not values['draftsperson']:
            values['draftsperson'] = values['surveyor']
        return values



