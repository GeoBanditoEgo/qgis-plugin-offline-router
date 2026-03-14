# -*- coding: utf-8 -*-
"""
Offline Router Plugin - Main plugin class
"""
import os
from qgis.PyQt.QtWidgets import QAction, QMessageBox
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtCore import Qt
from qgis.core import QgsProject


class OfflineRouterPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.action = None
        self.dialog = None
        self.map_tool = None

    def initGui(self):
        icon_path = os.path.join(self.plugin_dir, 'icons', 'icon.png')
        if not os.path.exists(icon_path):
            icon = QIcon()
        else:
            icon = QIcon(icon_path)

        self.action = QAction(icon, 'Offline Router', self.iface.mainWindow())
        self.action.setToolTip('Offline Router — find shortest driving route between waypoints')
        self.action.triggered.connect(self.run)

        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu('&Offline Router', self.action)

    def unload(self):
        self.iface.removePluginMenu('&Offline Router', self.action)
        self.iface.removeToolBarIcon(self.action)
        if self.map_tool:
            self.iface.mapCanvas().unsetMapTool(self.map_tool)
        if self.dialog:
            self.dialog.close()
        del self.action

    def run(self):
        from .dialog import RouterDialog

        # Check project CRS — only EPSG:4326 and EPSG:3857 are supported
        project_crs = QgsProject.instance().crs()
        auth_id = project_crs.authid()
        if auth_id not in ('EPSG:4326', 'EPSG:3857'):
            QMessageBox.warning(
                self.iface.mainWindow(),
                'Offline Router — Unsupported Projection',
                f'Your project projection is <b>{auth_id}</b> '
                f'({project_crs.description()}).<br><br>'
                'Offline Router only supports:<br>'
                '&nbsp;&nbsp;• <b>EPSG:4326</b> — WGS 84 (geographic)<br>'
                '&nbsp;&nbsp;• <b>EPSG:3857</b> — WGS 84 / Pseudo-Mercator (web mercator)<br><br>'
                'Please change your project CRS to one of the above and try again.'
            )
            return

        if self.dialog is None:
            self.dialog = RouterDialog(self.iface)
            self.dialog.finished.connect(self._on_dialog_closed)
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def _on_dialog_closed(self):
        self.dialog = None
