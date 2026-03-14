# -*- coding: utf-8 -*-
"""
Offline Router - QGIS Plugin
Find the shortest driving route between multiple waypoints using a local SpatiaLite routing database.
"""


def classFactory(iface):
    from .plugin import OfflineRouterPlugin
    return OfflineRouterPlugin(iface)
