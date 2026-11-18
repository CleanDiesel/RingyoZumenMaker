"""Data-defined drawing values, shared by the dock and Processing."""
from qgis.PyQt.QtCore import QDate, QDateTime
from qgis.core import (
    QgsExpression, QgsExpressionContext, QgsExpressionContextUtils,
    QgsProperty, QgsVariantUtils,
)
from .drawing_inputs import DrawingError, FIELDS


def property_value(literal, expression=''):
    if not expression.strip():
        return literal
    return {'value': literal, 'property': QgsProperty.fromExpression(expression).toVariant()}


def unpack(value):
    prop = QgsProperty()
    if isinstance(value, dict):
        if value.get('property') and not prop.loadVariant(value['property']):
            raise DrawingError('上書き設定を読み込めません')
        return value.get('value', ''), prop
    return value, prop


def context_for(layer):
    context = QgsExpressionContext()
    context.appendScopes(QgsExpressionContextUtils.globalProjectLayerScopes(layer))
    if layer is not None:
        context.setFields(layer.fields())
    return context


def evaluate(expression, context, description):
    expr = QgsExpression(expression)
    if expr.hasParserError():
        raise DrawingError(description + '：' + expr.parserErrorString())
    if not expr.prepare(context):
        # QGIS may populate its missing-field error only during evaluation.
        expr.evaluate(context)
        error = expr.evalErrorString() or '式を準備できませんでした'
        raise DrawingError(description + '：' + error + '（式：' + expression + '）')
    value = expr.evaluate(context)
    if expr.hasEvalError():
        raise DrawingError(description + '：' + expr.evalErrorString())
    return value


def text_value(value, key):
    if QgsVariantUtils.isNull(value):
        return ''
    if key.endswith('_date'):
        if isinstance(value, QDateTime):
            value = value.date()
        date = value if isinstance(value, QDate) else QDate.fromString(str(value), 'yyyy-MM-dd')
        if not date.isValid():
            raise DrawingError('日付はyyyy-MM-dd形式で指定してください：' + key + ' = ' + str(value))
        return date.toString('yyyy年MM月dd日')
    return str(value).strip()


def resolve_values(saved, pages, key, inherited=None):
    literal, prop = unpack(saved)
    if not prop.isActive():
        text = text_value(literal, key)
        return [text] if text else []
    expression = prop.asExpression()
    if expression == '@rz_land_' + key:
        if inherited is None:
            raise DrawingError('施行地自身には施行地からの継承を指定できません：' + key)
        return list(inherited)
    result = []
    for page in pages:
        layer = page.point_layer.currentLayer()
        if layer is None:
            raise DrawingError('上書きに使うポイントレイヤがありません：' + key)
        context = context_for(layer)
        filter_text = page.values().get('filter_exp', '').strip()
        for feature in layer.getFeatures():
            context.setFeature(feature)
            if filter_text and not evaluate(filter_text, context, '抽出条件'):
                continue
            text = text_value(evaluate(expression, context, key), key)
            if text and text not in result:
                result.append(text)
    if not pages:
        # Constant expressions are also useful for work-only drawings.
        text = text_value(evaluate(expression, context_for(None), key), key)
        if text:
            result.append(text)
    return result


def resolve_attributes(attributes, pages, inherited=None):
    values = {}
    for key, _, _ in FIELDS:
        default = QDate.currentDate().toString('yyyy-MM-dd') if key.endswith('_date') else ''
        values[key] = resolve_values(attributes.get(key, default), pages, key,
                                     inherited.get(key, []) if inherited is not None else None)
    if not values['drawing_company']:
        values['drawing_company'] = list(values['survey_company'])
    if not values['draftsperson']:
        values['draftsperson'] = list(values['surveyor'])
    if inherited is not None and not values['application_number']:
        values['application_number'] = list(inherited['application_number'])
    return values


def resolve_folder_name(saved, pages):
    values = resolve_values(saved, pages, 'output_folder_name')
    literal, prop = unpack(saved)
    if not values:
        if prop.isActive():
            raise DrawingError('出力フォルダ名の式から名前を取得できませんでした')
        return ''
    if len(values) != 1:
        raise DrawingError('出力フォルダ名が複数あります。抽出条件や式で一つの名前に絞ってください。')
    name = values[0]
    reserved = {'CON', 'PRN', 'AUX', 'NUL'} | {f'{prefix}{i}' for prefix in ('COM', 'LPT') for i in range(1, 10)}
    if (name in ('.', '..') or name.endswith(('.', ' ')) or len(name) > 255
            or any(char in '\\/:*?"<>|' or ord(char) < 32 for char in name)
            or name.split('.')[0].upper() in reserved):
        raise DrawingError('出力フォルダ名にはパスではなく、使用可能なフォルダ名を一つ指定してください：' + name)
    return name
