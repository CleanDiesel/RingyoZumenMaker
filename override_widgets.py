"""QGIS property buttons alongside literal drawing inputs."""
from qgis.PyQt.QtWidgets import QWidget, QHBoxLayout, QDateEdit
from qgis.PyQt.QtCore import QCoreApplication, QEvent, QObject
from qgis.core import QgsExpressionContextGenerator, QgsProperty, QgsPropertyDefinition
from qgis.gui import QgsPropertyOverrideButton
from .drawing_overrides import context_for, unpack
from .drawing_inputs import DrawingError


class DrawingExpressionContext(QgsExpressionContextGenerator):
    def __init__(self, layer_getter):
        super().__init__()
        self.layer_getter = layer_getter

    def createExpressionContext(self):
        return context_for(self.layer_getter())


def inherits_land_value(widget, key):
    prop = widget._drawing_override.toProperty()
    return prop.isActive() and prop.asExpression() == '@rz_land_' + key


def set_land_value_inheritance(widget, key, enabled):
    button = widget._drawing_override
    button.setToProperty(QgsProperty.fromExpression('@rz_land_' + key, enabled))
    button.changed.emit()


class OverrideMenuOptions(QObject):
    def __init__(self, menu, widget, key, inherit, copy_first):
        super().__init__(menu)
        self.widget = widget
        self.key = key
        self.inherit = inherit
        self.copy_first = copy_first
        menu.installEventFilter(self)

    def eventFilter(self, menu, event):
        if event.type() != QEvent.Type.Show:
            return False
        # QGIS rebuilds the native actions in aboutToShow.
        for action in menu.actions():
            if action.property('rz_custom_override'):
                menu.removeAction(action)
                action.deleteLater()
        if self.inherit:
            action = self.add_action(menu, '施行地の値を継承')
            action.setCheckable(True)
            action.setChecked(inherits_land_value(self.widget, self.key))
            action.triggered.connect(lambda checked: set_land_value_inheritance(self.widget, self.key, checked))
        if self.copy_first:
            source = self.copy_first()
            action = self.add_action(menu, '1つ目の付帯工作物の値で上書き')
            action.setEnabled(source is not None)
            if source is not None:
                action.triggered.connect(lambda: self.copy_field(source))
        return False

    @staticmethod
    def add_action(menu, label):
        before = next(iter(menu.actions()), None)
        action = menu.addAction(label)
        action.setProperty('rz_custom_override', True)
        menu.insertAction(before, action)
        return action

    def copy_field(self, source):
        button = self.widget._drawing_override
        button.setToProperty(source._drawing_override.toProperty())
        if isinstance(self.widget, QDateEdit):
            self.widget.setDate(source.date())
        else:
            self.widget.setText(source.text())
        button.changed.emit()


def override_row(widget, key, label, layer_getter, inherit=False, copy_first=None):
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    button = QgsPropertyOverrideButton(row)
    definition = QgsPropertyDefinition(key, label, QgsPropertyDefinition.StandardPropertyTemplate.String)
    button.init(0, QgsProperty(), definition, layer_getter(), False)
    generator = DrawingExpressionContext(layer_getter)
    button.registerExpressionContextGenerator(generator)
    button._drawing_context = generator
    button.setUsageInfo('対象測点の属性または式で上書きします。複数の値は重複を除いて併記します。')
    def label_expression_menu():
        edit_label = QCoreApplication.translate('QgsPropertyOverrideButton', 'Edit…')
        for action in button.menu().actions():
            if action.text() in (edit_label, 'Edit…', '編集…'):
                action.setText('式を入力・編集…')
    button.menu().aboutToShow.connect(label_expression_menu)
    button.changed.connect(lambda: widget.setEnabled(not button.isActive()))
    button.activated.connect(lambda *_: widget.setEnabled(not button.isActive()))
    if inherit or copy_first:
        button._menu_options = OverrideMenuOptions(button.menu(), widget, key, inherit, copy_first)
    layout.addWidget(widget, 1)
    layout.addWidget(button)
    widget._drawing_override = button
    return row


def saved_value(widget, literal):
    prop = widget._drawing_override.toProperty()
    return {'value': literal, 'property': prop.toVariant()} if prop.asExpression() else literal


def restore_value(widget, saved):
    try:
        literal, prop = unpack(saved)
    except DrawingError as error:
        raise ValueError(str(error)) from error
    widget._drawing_override.setToProperty(prop)
    widget.setEnabled(not prop.isActive())
    return literal
