"""Shared execution service for the dock, Processing, Python and batch jobs."""
import copy
from pathlib import Path
from qgis.core import QgsProject, QgsCoordinateReferenceSystem, QgsProcessingFeedback
from qgis.PyQt.QtCore import QDate
from .drawing_inputs import DrawingRequest, DrawingInput, InputValue, DrawingError, DrawingCancelled, FIELDS
from .drawing_core import DrawingOperations, RoleDrawingOperations
from .project_session import ProjectSession


class _Progress:
    def __init__(self, service): self.service = service
    def setValue(self, value):
        if value < 100: self.service.check_cancelled()
        self.service.feedback.setProgress(value)


class DrawingService(RoleDrawingOperations, DrawingOperations, ProjectSession):
    def __init__(self, request, feedback=None, preserve_active_project=False):
        self.request = request
        self.config = copy.deepcopy(request.config)
        self.feedback = feedback if feedback is not None else QgsProcessingFeedback()
        self.preserve_active_project = preserve_active_project
        self.progressBar = _Progress(self)
        self.success = False
        self.iface = None
        self.render_project = QgsProject()
        self.render_project.setTransformContext(QgsProject.instance().transformContext())
        self._editable_projects = None
        settings, output = self.config.get('map', {}), self.config.get('output', {})
        mapping = {'scale': ('scale', 1000), 'locationScale': ('location_scale', 5000),
                   'paper': ('paper', 'A4'), 'locationPaper': ('location_paper', 'A4'),
                   'ketasu': ('coordinate_decimals', 5), 'hyouOrder': ('xy_table_order', 0),
                   'lengthDecimals': ('length_display_decimals', 1), 'areaDecimals': ('area_display_decimals', 0),
                   'haDecimals': ('hectare_display_decimals', 2), 'isJochikeisan': ('show_deduction', False),
                   'minEx': ('minimum_exclusion_area_a', 1), 'isIchizu': ('create_location_map', False),
                   'ichizuDirection': ('location_direction', 0)}
        for name, (key, default) in mapping.items(): setattr(self, name, InputValue(settings.get(key, default)))
        self.crs = InputValue(QgsCoordinateReferenceSystem(settings.get('crs', 'EPSG:6678')))
        self.render_project.setCrs(self.crs.crs())
        self.fileName = InputValue(output.get('directory', ''))
        self.outputMode = InputValue(0 if output.get('create_qgz', True) else 1)
        self.backupQgz = InputValue(output.get('backup_qgz', False))
        self.land_attributes = self.config.get('drawing_attributes', {}).get('land', self.config.get('basic', {}))
        for key, _, name in FIELDS:
            value = self.land_attributes.get(key, QDate.currentDate().toString('yyyy-MM-dd') if key.endswith('_date') else '')
            if isinstance(value, dict): value = value.get('value', '')
            setattr(self, name, InputValue(value))
        self.role_pages = {key: [] for key in ('land', 'ex_polygon', 'ex_line', 'work')}
        for group, default in (('polygons', 'land'), ('lines', 'work')):
            for item in self.config.get(group, {}).get('items', []):
                role = item.get('role', default)
                if role not in self.role_pages: raise DrawingError('不明な入力区分です: ' + str(role))
                layer = request.layers.get(item['drawing_uid'])
                if layer is None: raise DrawingError('ポイントレイヤが見つかりません: ' + str(item.get('name', role)))
                record = DrawingInput(item, layer, role, len(self.role_pages[role]) + 1)
                self.role_pages[role].append(record)
        self.shuis = self.role_pages['land'] + self.role_pages['ex_polygon']
        self.haisuis = self.role_pages['ex_line'] + self.role_pages['work']
        for index, record in enumerate(self.shuis, 1): record.index = index
        for index, record in enumerate(self.haisuis, 1): record.index = index
        self.isShui, self.isHaisui = InputValue(bool(self.shuis)), InputValue(bool(self.haisuis))

    def run(self, test=False):
        self.check_cancelled()
        if not self.fileName.filePath(): raise DrawingError('出力フォルダを指定してください')
        if not self.role_pages['land'] and (self.role_pages['ex_polygon'] or self.role_pages['ex_line']):
            raise DrawingError('除地を指定する場合は施行地も指定してください')
        for record in self.haisuis:
            value = record.values()
            if value['has_haba'] and value['haba'] < .01:
                raise DrawingError('ラインの幅は0.01m以上を指定してください')
        for role, records in self.role_pages.items():
            if role == 'work':
                for record in records: self.attribute_values(role, [record])
        self.attribute_values('land', self.role_pages['land'])
        if not test: Path(self.fileName.filePath()).mkdir(parents=True, exist_ok=True)
        self.execute(test=test)
        if not self.success: raise DrawingError('実測図を作成できませんでした')
        result = {'OUTPUT': self.fileName.filePath()}
        if not test:
            result['HTML'] = str(Path(self.fileName.filePath()) / 'index.html')
            result['CONFIG'] = str(Path(self.fileName.filePath()) / 'input.config')
            if self.outputMode.currentIndex() == 0:
                result['PROJECT'] = str(Path(self.fileName.filePath()) / 'qgz' / self.project_file_name())
        return result

    def configuration_data(self): return copy.deepcopy(self.config)
    def check_cancelled(self):
        if self.feedback.isCanceled(): raise DrawingCancelled('処理をキャンセルしました')
    def append_output_log(self, message): self.feedback.pushInfo(str(message))
    def warn(self, message): self.feedback.pushWarning(str(message))
    def clear_output_log(self): pass
    def open_output_tab(self): pass
    def settle_project_edits(self): return True
    def project_file_name(self):
        return self.safe_file_name(self.rinshohan.text().strip() or '実測図') + '.qgz'
    def paper_factor(self): return 420 / 297 if self.paper.currentText() == 'A3' else 1.0
    @staticmethod
    def layer_reference(layer):
        if layer is None: return None
        return {'id': layer.id(), 'name': layer.name(), 'source': layer.source(), 'provider': layer.providerType()}

    def attribute_values(self, role, pages):
        attributes = self.land_attributes if role == 'land' else pages[0].literal_attributes
        values = {}
        for key, _, _ in FIELDS:
            value = attributes.get(key, QDate.currentDate().toString('yyyy-MM-dd') if key.endswith('_date') else '')
            if isinstance(value, dict): value = value.get('value', '')
            if key.endswith('_date'):
                date = QDate.fromString(str(value), 'yyyy-MM-dd')
                if not date.isValid(): raise DrawingError('日付の指定を確認してください: ' + key)
                text = date.toString('yyyy年MM月dd日')
            else: text = str(value or '').strip()
            values[key] = [text] if text else []
        if not values['drawing_company']: values['drawing_company'] = values['survey_company']
        if not values['draftsperson']: values['draftsperson'] = values['surveyor']
        return values

    def export_layout_image(self, layout, prefix):
        self.check_cancelled()
        return super().export_layout_image(layout, prefix)


def request_from_dock(dock):
    dock.sync_role_pages()
    return DrawingRequest(dock.configuration_data(), {
        page.drawing_uid: page.point_layer.currentLayer() for page in dock.shuis + dock.haisuis})
