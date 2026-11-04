# -*- coding: utf-8 -*-

def classFactory(iface):
    from .plugin import RingyoZumenMakerPlugin
    return RingyoZumenMakerPlugin(iface)
