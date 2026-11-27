"""Shared execution service for the dock, Processing, Python and batch jobs."""
import copy
from decimal import Decimal
from pathlib import Path
from qgis.core import QgsProject, QgsCoordinateReferenceSystem, QgsProcessingFeedback
from qgis.PyQt.QtCore import QDate
from qgis import utils as qgis_utils
from .drawing_inputs import DrawingRequest, DrawingInput, InputValue, DrawingError, DrawingCancelled, FIELDS
from .drawing_core import DrawingOperations, RoleDrawingOperations
from .project_session import ProjectSession
from .drawing_overrides import resolve_values, resolve_attributes, resolve_folder_name
from .polygon_groups import polygon_items
from .unified_project import source_key


class _Progress:
    def __init__(self, service): self.service = service
    def setValue(self, value):
        if value < 100: self.service.check_cancelled()
        self.service.feedback.setProgress(value)


class _CalculationFeedback(QgsProcessingFeedback):
    """Forward model messages and cancellation without forwarding model progress."""
    def __init__(self, feedback):
        super().__init__()
        self.target = feedback
        feedback.canceled.connect(self.cancel)
        if feedback.isCanceled():
            self.cancel()

    def reportError(self, error, fatalError=False):
        self.target.reportError(error, fatalError)

    def pushWarning(self, message):
        self.target.pushWarning(message)

    def pushDebugInfo(self, message):
        self.target.pushDebugInfo(message)

    def pushCommandInfo(self, message):
        self.target.pushCommandInfo(message)

    def pushConsoleInfo(self, message):
        self.target.pushConsoleInfo(message)

    def pushInfo(self, message):
        self.target.pushDebugInfo(message)

    def pushFormattedMessage(self, html, text):
        self.target.pushDebugInfo(text)


class DrawingService(RoleDrawingOperations, DrawingOperations, ProjectSession):
    def __init__(self, request, feedback=None, preserve_active_project=False, iface=None):
        self.request = request
        self.config = copy.deepcopy(request.config)
        self.feedback = feedback if feedback is not None else QgsProcessingFeedback()
        self._calculation_feedback = _CalculationFeedback(self.feedback)
        self.preserve_active_project = preserve_active_project
        self.progressBar = _Progress(self)
        self.success = False
        self.iface = iface if iface is not None else qgis_utils.iface
        self.render_project = QgsProject()
        self.render_project.setTransformContext(QgsProject.instance().transformContext())
        self._editable_projects = None
        self.output_subdirectory = ''
        self._preview_output_base = False
        self._skip_output_validation = False
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
                records = polygon_items(item, layer) if role in ('land', 'ex_polygon') else [item]
                for record_item in records:
                    record = DrawingInput(record_item, layer, role, len(self.role_pages[role]) + 1)
                    record.registration_uid = item['drawing_uid']
                    self.role_pages[role].append(record)
        self.shuis = self.role_pages['land'] + self.role_pages['ex_polygon']
        self.haisuis = self.role_pages['ex_line'] + self.role_pages['work']
        for index, record in enumerate(self.shuis, 1): record.index = index
        for index, record in enumerate(self.haisuis, 1): record.index = index
        self.isShui, self.isHaisui = InputValue(bool(self.shuis)), InputValue(bool(self.haisuis))

    def run(self, test=False, validate_output=True):
        self.check_cancelled()
        self._skip_output_validation = bool(test and not validate_output)
        self.fileName.saved = self.config.get('output', {}).get('directory', '')
        if not self.fileName.filePath() and not self._skip_output_validation:
            raise DrawingError('出力フォルダを指定してください')
        if not self.role_pages['land'] and (self.role_pages['ex_polygon'] or self.role_pages['ex_line']):
            raise DrawingError('除地を指定する場合は施行地も指定してください')
        land_names = []
        for record in self.role_pages['land']:
            names = resolve_values({'value': record.item.get('name', ''),
                                    'property': record.item.get('name_property', {})}, [record], 'name')
            record.name_edit.saved = '、'.join(names)
            land_names.extend(name for name in names if name not in land_names)
        for role in ('ex_polygon', 'ex_line', 'work'):
            for record in self.role_pages[role]:
                record.name_edit.saved = '、'.join(resolve_values(
                    {'value': record.item.get('name', ''), 'property': record.item.get('name_property', {})},
                    [record], 'name', land_names))
        self._land_attribute_values = resolve_attributes(self.land_attributes, self.role_pages['land'])
        self.rinshohan.saved = '、'.join(self._land_attribute_values['forest_compartment'])
        self.output_subdirectory = '' if self._skip_output_validation else resolve_folder_name(
            self.config.get('output', {}).get('folder_name', ''), self.role_pages['land'] or self.role_pages['work'])
        if self.output_subdirectory:
            self.fileName.saved = str(Path(self.fileName.filePath()) / self.output_subdirectory)
        self._preview_output_base = test and bool(self.output_subdirectory)
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
        self.log_calculation_summary(test)
        result = {'OUTPUT': self.fileName.filePath()}
        if not test:
            result['HTML'] = str(Path(self.fileName.filePath()) / 'index.html')
            result['CONFIG'] = str(Path(self.fileName.filePath()) / 'input.config')
            if self.outputMode.currentIndex() == 0:
                result['PROJECT'] = str(Path(self.fileName.filePath()) / 'qgz' / self.project_file_name())
        return result

    def validate_inputs(self):
        if not self._preview_output_base:
            return super().validate_inputs()
        actual = self.fileName.saved
        self.fileName.saved = self.config['output']['directory']
        try:
            return super().validate_inputs()
        finally:
            self.fileName.saved = actual

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
        return {'id': layer.id(), 'name': layer.name(), 'source': layer.source(),
                'provider': layer.providerType(), 'source_key': source_key(layer)}

    def attribute_values(self, role, pages):
        attributes = self.land_attributes if role == 'land' else pages[0].literal_attributes
        if role == 'land':
            return self._land_attribute_values
        return resolve_attributes(attributes, pages, self._land_attribute_values)

    def log_calculation_detail(self, message):
        self.feedback.pushDebugInfo(message)

    def calculation_feedback(self):
        return self._calculation_feedback

    def log_calculation_summary(self, trial):
        def number(text):
            return format(Decimal(text), ',f')

        def area(value):
            square_metres = self.format_area_int(value)
            hectares = self.format_decimal(value / 10000, max(4, self.haDecimals.value()))
            approximate = '約' if Decimal(hectares) * 10000 != Decimal(square_metres) else ''
            return f'{number(square_metres)} m²（{approximate}{number(hectares)} ha）'

        def length(value):
            return number(self.format_length(value)) + ' m'

        def label(page, index):
            return ' '.join(page.name_edit.text().split()) or f'#{index}'

        log = self.feedback.pushInfo
        log('')
        log('【試算結果】' if trial else '【計算結果】')
        lands = self.role_pages['land']
        exclusions = self.role_pages['ex_polygon']
        exclusion_lines = self.role_pages['ex_line']
        works = self.role_pages['work']
        gross = sum(page.area for page in lands)
        if lands:
            log(f'施行地（{len(lands)}件）')
            if len(lands) == 1 and lands[0].name_edit.text().strip():
                log('  名称：' + label(lands[0], 1))
            if len(lands) > 1:
                for index, page in enumerate(lands, 1):
                    log(f'  {label(page, index)}：面積 {area(page.area)} ／ 外周長 {length(page.length)}')
            log('  施行地面積' + ('（除地差引前）' if exclusions or exclusion_lines else '') + '：' + area(gross))
            log('  外周長の合計（穴の内周を除く）：' + length(sum(page.length for page in lands)))
        if exclusions or exclusion_lines:
            polygon_area = sum(page.area for page in exclusions)
            log('除地')
            if exclusions:
                log(f'  ポリゴン（{len(exclusions)}件）：' + area(polygon_area))
                log('  ポリゴンの外周長の合計（穴の内周を除く）：' + length(sum(page.length for page in exclusions)))
                if len(exclusions) > 1:
                    for index, page in enumerate(exclusions, 1):
                        log(f'    {label(page, index)}：面積 {area(page.area)} ／ 外周長 {length(page.length)}')
            line_area = 0
            if exclusion_lines:
                minimum = self.minimum_exclusion_area_m2()
                log('  ラインの最小除地面積：' + area(minimum))
                for index, page in enumerate(exclusion_lines, 1):
                    width = page.values()['haba']
                    raw = page.length * width
                    applied = raw >= minimum
                    if applied:
                        line_area += raw
                    status = '差引対象' if applied else '最小除地面積未満のため差引対象外'
                    log(f'  ライン {label(page, index)}：延長 {length(page.length)} × 全幅 {number(self.format_width(width))} m'
                        f' ＝ {area(raw)} ／ {status}')
                log('  ラインの差引面積合計：' + area(line_area))
            total = polygon_area + line_area
            log('  除地面積の合計：' + area(total))
            if lands:
                log('差引後の施行地面積：' + area(gross - total))
        if works:
            log(f'付帯工作物（{len(works)}件）')
            for index, page in enumerate(works, 1):
                values = page.values()
                suffix = ' ／ 幅 ' + number(self.format_width(values['haba'])) + ' m' if values['has_haba'] else ''
                log(f'  {label(page, index)}：延長 {length(page.length)}' + suffix)
            log('  延長の合計：' + length(sum(page.length for page in works)))
        if trial:
            log('試算のみ完了：ファイル出力をスキップしました。')
        log('')

    def export_layout_image(self, layout, prefix):
        self.check_cancelled()
        return super().export_layout_image(layout, prefix)


def request_from_dock(dock):
    dock.sync_role_pages()
    return DrawingRequest(dock.configuration_data(), {
        page.drawing_uid: page.point_layer.currentLayer() for page in dock.shuis + dock.haisuis})
