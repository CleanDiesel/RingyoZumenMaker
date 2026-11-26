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
    QgsProject, QgsProcessingUtils, QgsVectorLayer, QgsProperty,
)
from .drawing_inputs import DrawingRequest, DrawingError, FIELDS
from .drawing_service import DrawingService
from .unified_project import source_key
from .drawing_overrides import property_value, unpack

ALGORITHM_ID = 'ringyozumenmaker:create_drawings'
ROLES = (('LAND', 'land', '施行地'), ('EX_POLYGON', 'ex_polygon', '除地ポリゴン'),
         ('EX_LINE', 'ex_line', '除地ライン'), ('WORK', 'work', '付帯工作物'))


def column_value(literal, prop):
    """A single editable batch cell can hold a literal or deferred expression."""
    if prop.isActive():
        return 'expression:' + prop.asExpression()
    if isinstance(literal, str) and literal.startswith(('expression:', 'field:', 'literal:')):
        return 'literal:' + literal
    return literal


def column_input(text, legacy_expression=''):
    # Older optional expression widgets may serialize a cleared field as "".
    # It is an empty field reference, not a requested attribute override.
    if legacy_expression.strip() not in ('', '""'):
        return property_value(text, legacy_expression)
    if text.startswith('literal:'):
        return text[len('literal:'):]
    if text.startswith('expression:'):
        expression = text[len('expression:'):].strip()
        if not expression:
            raise DrawingError('expression: の後に式を指定してください')
        return property_value('', expression)
    if text.startswith('field:'):
        field = text[len('field:'):].strip()
        if not field:
            raise DrawingError('field: の後にフィールド名を指定してください')
        return property_value('', QgsProperty.fromField(field).asExpression())
    return text


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
                '実行／試算の列で行ごとに選択できます。試算は計算結果だけをログへ表示し、出力せず次の行へ進みます。'
                'バッチ画面ではレイヤ、幅、図面情報、出力先の各列にオートフィルを使えます。'
                '地物の区分は名前・計算をまとめる単位です。空欄なら対象を1地物として扱います。'
                '離れた輪郭はパートの区分で分けます。各パートを別々に閉じ、ラベル・面積・周長を地物単位にまとめます。'
                '名前・属性情報は各列のオートフィル「式で計算」「式で値を追加」で入力できます。'
                'これらの式は行ごとに評価します。測点属性を実行時に評価する場合は、セルに field:列名 または expression:式 を指定します。'
                '実行時の式は各組の対象測点で評価し、異なる値を重複なく併記します。'
                '付帯工作物の属性は expression:@rz_land_forest_owner などで施行地の値を継承できます。'
                '名前欄での継承は expression:@rz_land_name。日付の式は日付型またはyyyy-MM-dd形式を返してください。'
                '位置図の背景は現在のプロジェクトの表示レイヤを使います。')

    def initAlgorithm(self, config=None):
        run_mode = QgsProcessingParameterEnum('RUN_MODE', '実行／試算', options=['実行', '試算'], defaultValue=0)
        run_mode.setHelp('試算は計算結果だけをログに表示し、ファイル出力をスキップして次の行へ進みます。')
        self.addParameter(run_mode)
        for prefix, role, label in ROLES:
            self.addParameter(QgsProcessingParameterVectorLayer(prefix, label + 'のポイントレイヤ',
                              types=[QgsProcessing.TypeVectorPoint], optional=True))
            for suffix, title, default in (('FILTER', '抽出条件', ''), ('GROUP', '地物の区分の式（空欄なら1地物）', ''),
                                            ('LABEL', '測点名の式', '$id'), ('ORDER', '結合順の式', '$id')):
                self.addParameter(QgsProcessingParameterExpression(prefix + '_' + suffix, label + '：' + title,
                    defaultValue=default, parentLayerParameterName=prefix, optional=suffix in ('FILTER', 'GROUP')))
            self.addParameter(QgsProcessingParameterString(prefix + '_NAME', label + '：名称', defaultValue=label, optional=True))
            self.parameterDefinition(prefix + '_NAME').setHelp('オートフィルの「式で計算」「式で値を追加」に対応。測点属性の指定は field:列名、実行時の式は expression:式。')
            name_expression = QgsProcessingParameterString(prefix + '_NAME_EXPRESSION', label + '：名称の上書きの式', optional=True)
            name_expression.setHelp('空欄なら名称の直接入力値を使います。' +
                                    ('施行地の名称を継承する場合は @rz_land_name。' if role != 'land' else ''))
            name_expression.setFlags(name_expression.flags() | Qgis.ProcessingParameterFlag.Hidden)
            self.addParameter(name_expression)
            if role in ('land', 'ex_polygon'):
                part = QgsProcessingParameterExpression(prefix + '_PART', label + '：パートの区分の式（任意）',
                    parentLayerParameterName=prefix, optional=True)
                part.setHelp('同じポリゴンの離れた輪郭を分ける式です。名前・ラベル・面積・周長は地物単位にまとめます。')
                self.addParameter(part)
        self.addParameter(QgsProcessingParameterNumber('EX_LINE_WIDTH', '除地ライン：全幅（m）',
            type=QgsProcessingParameterNumber.Double, defaultValue=1, minValue=.01))
        self.addParameter(QgsProcessingParameterBoolean('WORK_HAS_WIDTH', '付帯工作物：幅を表示', defaultValue=True))
        self.addParameter(QgsProcessingParameterNumber('WORK_WIDTH', '付帯工作物：幅（m）',
            type=QgsProcessingParameterNumber.Double, defaultValue=1, minValue=.01))
        work_type = QgsProcessingParameterString('WORK_TYPE', '付帯工作物：種別', defaultValue='排水', optional=True)
        work_type.setFlags(work_type.flags() | Qgis.ProcessingParameterFlag.Hidden)
        self.addParameter(work_type)
        for prefix, title in (('LAND', '施行地・除地の図面'), ('WORK', '付帯工作物の図面')):
            for key, label, _ in FIELDS:
                default = QDate.currentDate().toString('yyyy-MM-dd') if key.endswith('_date') else ''
                param = QgsProcessingParameterString(prefix + '_' + key.upper(), title + '：' + label,
                    defaultValue=default, optional=not key.endswith('_date'))
                if key.endswith('_date'): param.setHelp('yyyy-MM-dd形式で入力します（例：2026-11-05）。')
                param.setHelp(param.help() + ' オートフィルの「式で計算」「式で値を追加」に対応。測点属性は field:列名、実行時の式は expression:式。' +
                              ('施行地から継承する場合は expression:@rz_land_' + key + '。' if prefix == 'WORK' else ''))
                self.addParameter(param)
                expr = QgsProcessingParameterString(prefix + '_' + key.upper() + '_EXPRESSION',
                    title + '：' + label + 'の上書きの式', optional=True)
                expr.setHelp('空欄なら直接入力値を使用。' +
                             ('施行地から継承する場合は @rz_land_' + key + '。' if prefix == 'WORK' else ''))
                expr.setFlags(expr.flags() | Qgis.ProcessingParameterFlag.Hidden)
                self.addParameter(expr)
        self.addParameter(QgsProcessingParameterBoolean('WORK_INHERIT_ALL', '付帯工作物：属性・測量情報をすべて施行地から継承（各欄の式・フィールド指定を優先）', defaultValue=False))
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
        output_folder = QgsProcessingParameterFolderDestination('OUTPUT', '出力フォルダ', optional=True)
        output_folder.setHelp('実行時は指定が必要です。試算時は出力先が空欄でも、フォルダを作成せず計算結果だけをログに表示します。')
        self.addParameter(output_folder)
        folder_name = QgsProcessingParameterString('OUTPUT_NAME', '出力フォルダ名（任意）', optional=True)
        folder_name.setHelp('空欄なら保存先に直接出力。指定時は保存先の下に同名のフォルダを作成します。オートフィルの式で計算・式で値を追加に対応。field:列名、expression:式は施行地の対象測点（施行地がなければ付帯工作物）を参照します。名前は一つに絞ってください。')
        self.addParameter(folder_name)
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
        for group_index, (group_label, clause) in enumerate(groups, 1):
            saved_name = column_input(self.parameterAsString(parameters, prefix + '_NAME', context),
                                      self.parameterAsString(parameters, prefix + '_NAME_EXPRESSION', context))
            name_literal, name_property = unpack(saved_name)
            name = str(name_literal).strip() if name_literal is not None else ''
            # Group values can be internal multipart/ring IDs. Keep them out of
            # drawing names; use short numbers only when several inputs exist.
            if name and len(groups) > 1: name += str(group_index)
            uid = uuid.uuid5(uuid.NAMESPACE_URL, role + ':' + source_key(layer) + ':' + expression + ':' + clause).hex
            selected_filter = ' AND '.join('(' + value + ')' for value in (filter_text, clause) if value)
            item = {'name': name, 'drawing_uid': uid, 'role': role, 'layer': DrawingService.layer_reference(layer),
                    'filter_expression': selected_filter,
                    'point_name_expressions': [self.parameterAsString(parameters, prefix + '_LABEL', context)],
                    'primary_point_name_index': 0,
                    'sort_expression': self.parameterAsString(parameters, prefix + '_ORDER', context),
                    'deduct_area': role in ('ex_polygon', 'ex_line')}
            item['name_property'] = name_property.toVariant() if name_property.isActive() else {}
            if role in ('land', 'ex_polygon'):
                item['part_expression'] = self.parameterAsString(parameters, prefix + '_PART', context)
            if role in ('ex_line', 'work'):
                item.update(width=self.parameterAsDouble(parameters, 'EX_LINE_WIDTH' if role == 'ex_line' else 'WORK_WIDTH', context),
                            has_width=role == 'ex_line' or self.parameterAsBool(parameters, 'WORK_HAS_WIDTH', context),
                            type=self.parameterAsString(parameters, 'WORK_TYPE', context) if role == 'work' else '除地ライン')
            if role == 'work': item['drawing_attributes'] = self.literal_attributes(parameters, context, 'WORK')
            items.append(item); layers[uid] = layer
        return items, layers

    def literal_attributes(self, parameters, context, prefix):
        attributes = {}
        inherit = prefix == 'WORK' and self.parameterAsBool(parameters, 'WORK_INHERIT_ALL', context)
        for key, _, _ in FIELDS:
            name = prefix + '_' + key.upper()
            expression = self.parameterAsString(parameters, name + '_EXPRESSION', context)
            saved = column_input(self.parameterAsString(parameters, name, context), expression)
            if inherit and not unpack(saved)[1].isActive():
                saved = property_value(saved, '@rz_land_' + key)
            literal, prop = unpack(saved)
            if key.endswith('_date') and prop.isActive() and not literal:
                saved = {'value': QDate.currentDate().toString('yyyy-MM-dd'), 'property': prop.toVariant()}
            attributes[key] = saved
        return attributes

    def request(self, parameters, context):
        output = self.parameterAsFileOutput(parameters, 'OUTPUT', context)
        dock = self.parameterAsString(parameters, 'DOCK_INPUTS', context)
        if dock:
            try: config = json.loads(dock)
            except (ValueError, TypeError) as error: raise DrawingError('通常画面の入力を読み込めません: ' + str(error))
            if not isinstance(config, dict) or config.get('format') != 'RingyoZumenMaker.config' or config.get('mode') != 'measurement':
                raise DrawingError('実測図の入力設定ではありません')
            config['output']['directory'] = output
            if 'OUTPUT_NAME' in parameters:
                config['output']['folder_name'] = column_input(self.parameterAsString(parameters, 'OUTPUT_NAME', context))
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
                  'attribute_input_mode': 'data_defined_per_drawing',
                  'polygons': {'items': [], 'enabled': False}, 'lines': {'items': [], 'enabled': False},
                  'drawing_attributes': {'land': self.literal_attributes(parameters, context, 'LAND')},
                  'output': {'directory': output, 'create_qgz': self.parameterAsBool(parameters, 'CREATE_PROJECT', context),
                             'folder_name': column_input(self.parameterAsString(parameters, 'OUTPUT_NAME', context)),
                             'backup_qgz': self.parameterAsBool(parameters, 'BACKUP', context)}}
        config['basic'] = {key: unpack(value)[0] for key, value in config['drawing_attributes']['land'].items()}
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
            trial = self.parameterAsEnum(parameters, 'RUN_MODE', context) == 1
            request_parameters = dict(parameters)
            if trial:
                # No output settings need evaluating for a row that creates no files.
                request_parameters['OUTPUT_NAME'] = ''
                feedback.pushInfo('【試算のみ】計算結果をログに表示します。ファイル出力はスキップします。')
            service = DrawingService(self.request(request_parameters, context), feedback=feedback)
            result = service.run(test=trial, validate_output=not trial)
            return {} if trial else result
        except DrawingError as error:
            raise QgsProcessingException(str(error)) from error


class DrawingProvider(QgsProcessingProvider):
    def id(self): return 'ringyozumenmaker'
    def name(self): return 'RingyoZumenMaker'
    def longName(self): return 'RingyoZumenMaker 実測図'
    def icon(self): return QIcon(str(Path(__file__).parent / 'icon.png'))
    def loadAlgorithms(self):
        from .processing_compat import install_batch_html_compat
        install_batch_html_compat()
        self.addAlgorithm(DrawingAlgorithm())

    def unload(self):
        from .processing_compat import remove_batch_html_compat
        remove_batch_html_compat()
        super().unload()


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
    literal, prop = unpack(output.get('folder_name', ''))
    params['OUTPUT_NAME'] = column_value(literal, prop)
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
        if role in ('land', 'ex_polygon'):
            params[prefix + '_PART'] = item.get('part_expression', '')
            params[prefix + '_GROUP'] = item.get('object_expression', '')
        prop = QgsProperty()
        prop.loadVariant(item.get('name_property', {}))
        params[prefix + '_NAME'] = column_value(item.get('name', ''), prop)
        if role == 'ex_line': params['EX_LINE_WIDTH'] = item['width']
        if role == 'work':
            params.update(WORK_WIDTH=item['width'], WORK_HAS_WIDTH=item['has_width'])
            for key, _, _ in FIELDS:
                literal, prop = unpack(item.get('drawing_attributes', {}).get(key, ''))
                params['WORK_' + key.upper()] = column_value(literal, prop)
    for key, _, _ in FIELDS:
        literal, prop = unpack(config['drawing_attributes']['land'].get(key, ''))
        params['LAND_' + key.upper()] = column_value(literal, prop)
    if complex_inputs:
        params['DOCK_INPUTS'] = json.dumps(config, ensure_ascii=False)
    return params
