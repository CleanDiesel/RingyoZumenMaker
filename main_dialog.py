# -*- coding: utf-8 -*-

from qgis.PyQt.QtCore import QCoreApplication, Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction

from .main import Main
import os

class mainDialogPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.dialog = None
        self.plugin_dir = os.path.dirname(__file__)

    def tr(self, message):
        return QCoreApplication.translate("main_dialog.ui", message)

    def initGui(self):
        icon_path = os.path.join(self.plugin_dir, "icon.png")
        self.action = QAction(QIcon(icon_path), self.tr("Minimum Dialog"), self.iface.mainWindow())
        self.action.setCheckable(True)
        self.action.triggered.connect(self.run)

        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu(self.tr("&Minimum Dialog"), self.action)

    def unload(self):
        if self.dialog is not None:
            self.iface.removeDockWidget(self.dialog)
            self.dialog.deleteLater()
            self.dialog = None

        self.iface.removePluginMenu(self.tr("&Minimum Dialog"), self.action)
        self.iface.removeToolBarIcon(self.action)
        self.action = None

    def run(self, checked=True):
        if self.dialog is None:
            self.dialog = Main(self.iface.mainWindow(), iface=self.iface)
            self.dialog.visibilityChanged.connect(self.action.setChecked)
            try:
                dock_area = Qt.DockWidgetArea.RightDockWidgetArea
            except AttributeError:
                dock_area = Qt.RightDockWidgetArea
            self.iface.addDockWidget(dock_area, self.dialog)

        self.dialog.setVisible(checked)
        if checked:
            self.dialog.raise_()
