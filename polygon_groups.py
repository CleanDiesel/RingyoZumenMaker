"""Separate logical polygon identities from their individual closed parts."""
from copy import deepcopy
import uuid
from qgis.core import QgsExpression, QgsVariantUtils
from .drawing_overrides import context_for, evaluate
from .drawing_inputs import DrawingError


def polygon_items(item, layer):
    expression = item.get('object_expression', '').strip()
    if not expression:
        return [item]
    context = context_for(layer)
    filter_text = item.get('filter_expression', '').strip()
    clauses, seen = [], set()
    for feature in layer.getFeatures():
        context.setFeature(feature)
        if filter_text and not evaluate(filter_text, context, '抽出条件'):
            continue
        value = evaluate(expression, context, '地物の区分')
        identity = repr(value)
        if identity in seen:
            continue
        seen.add(identity)
        clause = f'({expression}) IS NULL' if QgsVariantUtils.isNull(value) else f'({expression}) = {QgsExpression.quotedValue(value)}'
        clauses.append(clause)
    if not clauses:
        raise DrawingError('地物の区分に該当する測点がありません')
    items = []
    for number, clause in enumerate(clauses, 1):
        grouped = deepcopy(item)
        grouped['drawing_uid'] = uuid.uuid5(uuid.NAMESPACE_URL, item['drawing_uid'] + ':' + clause).hex
        grouped['object_expression'] = ''
        grouped['filter_expression'] = ' AND '.join(f'({text})' for text in (filter_text, clause) if text)
        name = str(item.get('name') or '').strip()
        grouped['name'] = name + str(number) if name and len(clauses) > 1 else name
        items.append(grouped)
    return items
