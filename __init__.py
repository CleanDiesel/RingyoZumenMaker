# -*- coding: utf-8 -*-

def classFactory(iface):
    from .main_dialog import mainDialogPlugin
    return mainDialogPlugin(iface)
