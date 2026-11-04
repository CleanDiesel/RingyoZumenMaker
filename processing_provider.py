"""Native QGIS Processing entry point with individually autofillable parameters."""
import copy
import json
import uuid
from pathlib import Path
from qgis.PyQt.QtCore import QDate
from qgis.PyQt.QtGui import QIcon
from qgis.core import (
    Qgis, QgsProcessing, QgsProcessingAlgorithm, QgsProcessingProvider,
    QgsProcessingParameterVectorLayer, QgsProcessingParameterExpression,
    QgsProcessingParameterString, QgsProcessingParameterNumber,
    QgsProcessingParameterBoolean, QgsProcessingParameterCrs,
    QgsProcessingParameterEnum, QgsProcessingParameterFolderDestination,
    QgsProcessingOutputHtml, QgsProcessingOutputFile, QgsProcessingException,
    QgsExpression, QgsExpressionContext, QgsExpressionContextUtils, QgsVariantUtils,
    QgsProject, QgsProcessingUtils, QgsVectorLayer,
)
from .drawing_inputs import DrawingRequest, DrawingError, FIELDS
from .drawing_service import DrawingService
from .unified_project import source_key

ALGORITHM_ID = 'ringyozumenmaker:create_drawings'
ROLES = (('LAND', 'land', '施行地'), ('EX_POLYGON', 'ex_polygon', '除地ポリゴン'),
         ('EX_LINE', 'ex_line', '除地ライン'), ('WORK', 'work', '付帯工作物'))


class DrawingAlgorithm(QgsProcessingAlgorithm):
    def name(self): return 'create_drawings'
    def displayName(self): return '実測図を作成'
    def group(self): return '実測図'
    def groupId(self): return 'survey'
    def createInstance(self): return DrawingAlgorithm()
    def icon(self): return QIcon(str(Path(__file__).parent / 'icon.png'))
    def flags(self): return super().flags() | Qgis.ProcessingAlgorithmFlag.NoThreading
    def asPythonCommand(self, parameters, context):
        values = dict(parameters)
        # A memory layer's provider URI contains no feature data. Reference the
        # open layer by ID, as the standard layer selectors do.
        for prefix, _, _ in ROLES:
            layer = values.get(prefix)
            if isinstance(layer, QgsVectorLayer) and layer.providerType() == 'memory':
                values[prefix] = layer.id()
        command = super().asPythonCommand(values, context)
        for prefix, _, _ in ROLES:
            if prefix not in values:
                continue
            layer = self.parameterAsVectorLayer(values, prefix, context)
            if layer is not None and layer.providerType() == 'memory':
                literal = self.parameterDefinition(prefix).valueAsPythonString(values[prefix], context)
                command = command.replace(literal, repr(layer.id()))
        return command
    def shortHelpString(self):
        return ('施行地と除地は1図面、付帯工作物は区分ごとに実測図を作成します。'
                'バッチ画面ではレイヤ、幅、図面情報、出力先の各列にオートフィルを使えます。'
                '区分の式を空欄にすると、そのレイヤの対象測点を1組として結びます。'
                '区分の式には地物を分けるフィールドなどを指定してください。'
                '測量者・所有者などの情報は固定文字列です。ポイント属性は参照しません。'
                '同じ行から作る複数の付帯工作物には、その行の付帯工作物情報を使います。'
                '図面ごとに情報を変える場合はバッチの行を分け、出力フォルダも分けてください。'
                '位置図の背景は現在のプロジェクトの表示レイヤを使います。')

    def initAlgorithm(self, config=None):
        for prefix, role, label in ROLES:
            self.addParameter(QgsProcessingParameterVectorLayer(prefix, label + 'のポイントレイヤ',
                              types=[QgsProcessing.TypeVectorPoint], optional=True))
            for suffix, title, default in (('FILTER', '抽出条件', ''), ('GROUP', '区分の式（空欄なら1組）', ''),
                                            ('LABEL', '測点名の式', '$id'), ('ORDER', '結合順の式', '$id')):
                self.addParameter(QgsProcessingParameterExpression(prefix + '_' + suffix, label + '：' + title,
                    defaultValue=default, parentLayerParameterName=prefix, optional=suffix in ('FILTER', 'GROUP')))
            self.addParameter(QgsProcessingParameterString(prefix + '_NAME', label + '：名称', defaultValue=label, optional=True))
        self.addParameter(QgsProcessingParameterNumber('EX_LINE_WIDTH', '除地ライン：全幅（m）',
            type=QgsProcessingParameterNumber.Double, defaultValue=1, minValue=.01))
        self.addParameter(QgsProcessingParameterBoolean('WORK_HAS_WIDTH', '付帯工作物：幅を表示', defaultValue=True))
        self.addParameter(QgsProcessingParameterNumber('WORK_WIDTH', '付帯工作物：幅（m）',
            type=QgsProcessingParameterNumber.Double, defaultValue=1, minValue=.01))
        self.addParameter(QgsProcessingParameterString('WORK_TYPE', '付帯工作物：種別', defaultValue='排水', optional=True))
        for prefix, title in (('LAND', '施行地・除地の図面'), ('WORK', '付帯工作物の図面')):
            for key, label, _ in FIELDS:
                default = QDate.currentDate().toString('yyyy-MM-dd') if key.endswith('_date') else ''
                param = QgsProcessingParameterString(prefix + '_' + key.upper(), title + '：' + label,
                    defaultValue=default, optional=not key.endswith('_date'))
                if key.endswith('_date'): param.setHelp('yyyy-MM-dd形式で入力します（例：2026-11-05）。')
                self.addParameter(param)
        self.addParameter(QgsProcessingParameterCrs('CRS', '座標参照系', defaultValue='ProjectCrs'))
        self.addParameter(QgsProcessingParameterEnum('PAPER', '実測図の用紙サイズ', options=['A4', 'A3'], defaultValue=0))
        self.addParameter(QgsProcessingParameterNumber('SCALE', '実測図の縮尺の分母', type=QgsProcessingParameterNumber.Double, defaultValue=1000, minValue=1))
        self.addParameter(QgsProcessingParameterBoolean('SHOW_CALCULATIONS', '施行地・除地の図面に計算を表示', defaultValue=True))
        self.addParameter(QgsProcessingParameterNumber('MIN_EXCLUSION_A', '最小除地面積（a）', type=QgsProcessingParameterNumber.Double, defaultValue=1, minValue=0))
        for key, label, default in (('COORDINATE_DECIMALS', '座標表示の小数桁数', 5),
                                    ('LENGTH_DECIMALS', '延長表示の小数桁数', 1),
                                    ('AREA_DECIMALS', '面積（m²）表示の小数桁数', 0),
                                    ('HECTARE_DECIMALS', '面積（ha）表示の小数桁数', 2)):
            self.addParameter(QgsProcessingParameterNumber(key, label, type=QgsProcessingParameterNumber.Integer,
                                                          defaultValue=default, minValue=0, maxValue=9))
        self.addParameter(QgsProcessingParameterEnum('TABLE_ORDER', '座標表の測点並べ替え', options=['しない', '昇順', '降順'], defaultValue=0))
        self.addParameter(QgsProcessingParameterBoolean('LOCATION', '位置図を作成', defaultValue=False))
        self.addParameter(QgsProcessingParameterEnum('LOCATION_PAPER', '位置図の用紙サイズ', options=['A4', 'A3'], defaultValue=0))
        self.addParameter(QgsProcessingParameterEnum('LOCATION_DIRECTION', '位置図の向き', options=['縦', '横'], defaultValue=0))
        self.addParameter(QgsProcessingParameterNumber('LOCATION_SCALE', '位置図の縮尺の分母', type=QgsProcessingParameterNumber.Double, defaultValue=5000, minValue=1))
        self.addParameter(QgsProcessingParameterBoolean('CREATE_PROJECT', '編集用QGZ・GPKGを作成', defaultValue=True))
        self.addParameter(QgsProcessingParameterBoolean('BACKUP', '既存の生成ファイルをバックアップ', defaultValue=False))
        self.addParameter(QgsProcessingParameterFolderDestination('OUTPUT', '出力フォルダ'))
        # The dock can contain arbitrary numbers of registrations with different
        # layers and independent metadata. Preserve them in copied commands while
        # keeping the normal batch interface fully column based.
        complete = QgsProcessingParameterString('DOCK_INPUTS', '通常画面の複数登録（指定時は各列の入力より優先）', optional=True)
        complete.setHelp('通常画面からコピーした複数の入力を保持します。各列を直接編集する場合は、この詳細入力を空にしてください。出力フォルダは常に出力先の指定を使います。')
        complete.setFlags(complete.flags() | Qgis.ProcessingParameterFlag.Advanced)
        self.addParameter(complete)
        self.addOutput(QgsProcessingOutputHtml('HTML', '実測図'))
        self.addOutput(QgsProcessingOutputFile('CONFIG', '入力設定'))
        self.addOutput(QgsProcessingOutputFile('PROJECT', '編集用QGZ'))

    def grouped_inputs(self, parameters, context, prefix, role, label):
        layer = self.parameterAsVectorLayer(parameters, prefix, context)
        if layer is None: return [], {}
        expression = self.parameterAsString(parameters, prefix + '_GROUP', context).strip()
        filter_text = self.parameterAsString(parameters, prefix + '_FILTER', context).strip()
        groups = [('', '')]
        if expression:
            group_expression = QgsExpression(expression)
            filter_expression = QgsExpression(filter_text) if filter_text else None
            expr_context = QgsExpressionContext()
            expr_context.appendScopes(QgsExpressionContextUtils.globalProjectLayerScopes(layer))
            for expr in (group_expression, filter_expression):
                if expr and expr.hasParserError(): raise DrawingError(expr.parserErrorString())
            groups, seen = [], set()
            for feature in layer.getFeatures():
                expr_context.setFeature(feature)
                if filter_expression:
                    accepted = filter_expression.evaluate(expr_context)
                    if filter_expression.hasEvalError(): raise DrawingError(filter_expression.evalErrorString())
                    if not accepted: continue
                value = group_expression.evaluate(expr_context)
                if group_expression.hasEvalError(): raise DrawingError(group_expression.evalErrorString())
                identity = repr(value)
                if identity in seen: continue
                seen.add(identity)
                clause = '(' + expression + ') IS NULL' if QgsVariantUtils.isNull(value) else '(' + expression + ') = ' + QgsExpression.quotedValue(value)
                groups.append(('未指定' if QgsVariantUtils.isNull(value) else str(value), clause))
            if not groups: raise DrawingError(label + 'の対象測点がありません')
        items, layers = [], {}
        for group_label, clause in groups:
            name = self.parameterAsString(parameters, prefix + '_NAME', context).strip() or label
            if group_label: name += ' ' + group_label
            uid = uuid.uuid5(uuid.NAMESPACE_URL, role + ':' + source_key(layer) + ':' + expression + ':' + clause).hex
            selected_filter = ' AND '.join('(' + value + ')' for value in (filter_text, clause) if value)
            item = {'name': name, 'drawing_uid': uid, 'role': role, 'layer': DrawingService.layer_reference(layer),
                    'filter_expression': selected_filter,
                    'point_name_expressions': [self.parameterAsString(parameters, prefix + '_LABEL', context)],
                    'primary_point_name_index': 0,
                    'sort_expression': self.parameterAsString(parameters, prefix + '_ORDER', context),
                    'deduct_area': role in ('ex_polygon', 'ex_line')}
            if role in ('ex_line', 'work'):
                item.update(width=self.parameterAsDouble(parameters, 'EX_LINE_WIDTH' if role == 'ex_line' else 'WORK_WIDTH', context),
                            has_width=role == 'ex_line' or self.parameterAsBool(parameters, 'WORK_HAS_WIDTH', context),
                            type=self.parameterAsString(parameters, 'WORK_TYPE', context) if role == 'work' else '除地ライン')
            if role == 'work': item['drawing_attributes'] = self.literal_attributes(parameters, context, 'WORK')
            items.append(item); layers[uid] = layer
        return items, layers

    def literal_attributes(self, parameters, context, prefix):
        return {key: self.parameterAsString(parameters, prefix + '_' + key.upper(), context) for key, _, _ in FIELDS}

    def request(self, parameters, context):
        output = self.parameterAsFileOutput(parameters, 'OUTPUT', context)
        dock = self.parameterAsString(parameters, 'DOCK_INPUTS', context)
        if dock:
            try: config = json.loads(dock)
            except (ValueError, TypeError) as error: raise DrawingError('通常画面の入力を読み込めません: ' + str(error))
            if not isinstance(config, dict) or config.get('format') != 'RingyoZumenMaker.config' or config.get('mode') != 'measurement':
                raise DrawingError('実測図の入力設定ではありません')
            config['output']['directory'] = output
            layers = {}
            for group in ('polygons', 'lines'):
                for item in config.get(group, {}).get('items', []):
                    ref = item.get('layer') or {}
                    layer = QgsProcessingUtils.mapLayerFromString(ref.get('id', ''), context)
                    if layer is None:
                        layer = QgsProcessingUtils.mapLayerFromString(ref.get('source', ''), context)
                    if layer is None: raise DrawingError('ポイントレイヤが見つかりません: ' + ref.get('name', ''))
                    layers[item['drawing_uid']] = layer
            return DrawingRequest(config, layers)
        config = {'format': 'RingyoZumenMaker.config', 'version': 3, 'mode': 'measurement', 'workflow': 'roles',
                  'attribute_input_mode': 'literal_per_drawing',
                  'polygons': {'items': [], 'enabled': False}, 'lines': {'items': [], 'enabled': False},
                  'drawing_attributes': {'land': self.literal_attributes(parameters, context, 'LAND')},
                  'output': {'directory': output, 'create_qgz': self.parameterAsBool(parameters, 'CREATE_PROJECT', context),
                             'backup_qgz': self.parameterAsBool(parameters, 'BACKUP', context)}}
        config['basic'] = copy.deepcopy(config['drawing_attributes']['land'])
        config['map'] = {
            'crs': self.parameterAsCrs(parameters, 'CRS', context).authid(),
            'paper': ['A4', 'A3'][self.parameterAsEnum(parameters, 'PAPER', context)],
            'scale': self.parameterAsDouble(parameters, 'SCALE', context),
            'show_deduction': self.parameterAsBool(parameters, 'SHOW_CALCULATIONS', context),
            'minimum_exclusion_area_a': self.parameterAsDouble(parameters, 'MIN_EXCLUSION_A', context),
            'coordinate_decimals': self.parameterAsInt(parameters, 'COORDINATE_DECIMALS', context),
            'xy_table_order': self.parameterAsEnum(parameters, 'TABLE_ORDER', context),
            'length_display_decimals': self.parameterAsInt(parameters, 'LENGTH_DECIMALS', context),
            'area_display_decimals': self.parameterAsInt(parameters, 'AREA_DECIMALS', context),
            'hectare_display_decimals': self.parameterAsInt(parameters, 'HECTARE_DECIMALS', context),
            'create_location_map': self.parameterAsBool(parameters, 'LOCATION', context),
            'location_paper': ['A4', 'A3'][self.parameterAsEnum(parameters, 'LOCATION_PAPER', context)],
            'location_direction': self.parameterAsEnum(parameters, 'LOCATION_DIRECTION', context),
            'location_scale': self.parameterAsDouble(parameters, 'LOCATION_SCALE', context)}
        layers = {}
        for prefix, role, label in ROLES:
            items, sources = self.grouped_inputs(parameters, context, prefix, role, label)
            config['polygons' if role in ('land', 'ex_polygon') else 'lines']['items'].extend(items)
            layers.update(sources)
        for group in ('polygons', 'lines'): config[group]['enabled'] = bool(config[group]['items'])
        return DrawingRequest(config, layers)

    def processAlgorithm(self, parameters, context, feedback):
        try:
            service = DrawingService(self.request(parameters, context), feedback=feedback)
            return service.run()
        except DrawingError as error:
            raise QgsProcessingException(str(error)) from error


class DrawingProvider(QgsProcessingProvider):
    def id(self): return 'ringyozumenmaker'
    def name(self): return 'RingyoZumenMaker'
    def longName(self): return 'RingyoZumenMaker 実測図'
    def icon(self): return QIcon(str(Path(__file__).parent / 'icon.png'))
    def loadAlgorithms(self): self.addAlgorithm(DrawingAlgorithm())


def parameters_from_dock(dock):
    config = dock.configuration_data()
    settings, output = config['map'], config['output']
    params = {'OUTPUT': output['directory'], 'CRS': settings['crs'],
              'PAPER': int(settings['paper'] == 'A3'), 'SCALE': settings['scale'],
              'SHOW_CALCULATIONS': settings['show_deduction'], 'MIN_EXCLUSION_A': settings['minimum_exclusion_area_a'],
              'COORDINATE_DECIMALS': settings['coordinate_decimals'], 'LENGTH_DECIMALS': settings['length_display_decimals'],
              'AREA_DECIMALS': settings['area_display_decimals'], 'HECTARE_DECIMALS': settings['hectare_display_decimals'],
              'TABLE_ORDER': settings['xy_table_order'], 'LOCATION': settings['create_location_map'],
              'LOCATION_PAPER': int(settings['location_paper'] == 'A3'), 'LOCATION_DIRECTION': settings['location_direction'],
              'LOCATION_SCALE': settings['location_scale'], 'CREATE_PROJECT': output['create_qgz'], 'BACKUP': output['backup_qgz']}
    all_items = config['polygons']['items'] + config['lines']['items']
    complex_inputs = False
    for prefix, role, _ in ROLES:
        items = [item for item in all_items if item.get('role') == role]
        complex_inputs |= len(items) > 1
        if not items:
            continue
        item = items[0]
        labels = item.get('point_name_expressions', [])
        complex_inputs |= len(labels) > 1
        ref = item.get('layer') or {}
        params[prefix] = ref.get('id') or ref.get('source') or ''
        params[prefix + '_FILTER'] = item.get('filter_expression', '')
        params[prefix + '_LABEL'] = labels[item.get('primary_point_name_index', 0)] if labels else item.get('point_name_expression', '$id')
        params[prefix + '_ORDER'] = item.get('sort_expression', '$id')
        params[prefix + '_NAME'] = item.get('name', '')
        if role == 'ex_line': params['EX_LINE_WIDTH'] = item['width']
        if role == 'work':
            params.update(WORK_WIDTH=item['width'], WORK_HAS_WIDTH=item['has_width'], WORK_TYPE=item['type'])
            for key, _, _ in FIELDS: params['WORK_' + key.upper()] = item.get('drawing_attributes', {}).get(key, '')
    for key, _, _ in FIELDS:
        params['LAND_' + key.upper()] = config['drawing_attributes']['land'].get(key, '')
    if complex_inputs:
        params['DOCK_INPUTS'] = json.dumps(config, ensure_ascii=False)
    return params
