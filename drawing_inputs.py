"""Values passed to the drawing service, independent of input widgets."""
from dataclasses import dataclass
from copy import deepcopy
from qgis.PyQt.QtCore import QDate

FIELDS = (
    ('survey_date', '測量日', 'sokuryobi'),
    ('survey_company', '測量事業者名', 'sokuryojigyosha'),
    ('surveyor', '測量者名', 'sokuryosha'),
    ('drawing_date', '製図日', 'seizubi'),
    ('drawing_company', '製図事業者名', 'seizujigyosha'),
    ('draftsperson', '製図者名', 'seizusha'),
    ('forest_compartment', '林小班', 'rinshohan'),
    ('forest_owner', '山林所有者名', 'sanrinshoyusha'),
)


class DrawingError(Exception):
    pass


class DrawingCancelled(DrawingError):
    pass


@dataclass
class DrawingRequest:
    config: dict
    layers: dict


class InputValue:
    """Read saved values through the existing calculation interface, without Qt controls."""
    def __init__(self, value):
        self.saved = value

    def text(self): return str(self.saved) if self.saved is not None else ''
    def currentText(self): return self.text()
    def expression(self): return self.text()
    def filePath(self): return self.text()
    def value(self): return self.saved
    def scale(self): return float(self.saved)
    def crs(self): return self.saved
    def currentLayer(self): return self.saved
    def currentIndex(self): return int(self.saved)
    def isChecked(self): return bool(self.saved)
    def setChecked(self, value): self.saved = bool(value)
    def date(self): return QDate.fromString(self.text(), 'yyyy-MM-dd')


class DrawingInput:
    def __init__(self, item, layer, role, index):
        self.item = deepcopy(item)
        self.role, self.index = role, index
        self.drawing_uid = item['drawing_uid']
        self.point_layer = InputValue(layer)
        self.name_edit = InputValue(item.get('name', ''))
        self.literal_attributes = deepcopy(item.get('drawing_attributes', {}))
        self.length = self.area = None
        self.pt_layer = self.line_layer = self.area_layer = None
        self.xy_table_rows = []

    def values(self):
        labels = self.item.get('point_name_expressions') or [self.item.get('point_name_expression') or '$id']
        primary = int(self.item.get('primary_point_name_index', 0))
        has_width = self.role == 'ex_line' or bool(self.item.get('has_width', True))
        return {'name': self.name_edit.text(), 'type': self.item.get('type', '排水'),
                'point_layer': self.point_layer.currentLayer(),
                'filter_exp': self.item.get('filter_expression', ''),
                'sort_exp': self.item.get('sort_expression', '$id'),
                'sokuten_label_exp': labels[primary], 'sokuten_label_expressions': labels,
                'primary_label_index': primary, 'haba': float(self.item.get('width', 1)),
                'has_haba': has_width, 'is_jochi': self.role in ('ex_polygon', 'ex_line')}
