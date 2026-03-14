# -*- coding: utf-8 -*-
"""
Map tool for capturing click points on the canvas.
"""
from qgis.gui import QgsMapTool, QgsVertexMarker
from qgis.core import QgsPointXY, QgsWkbTypes
from qgis.PyQt.QtCore import pyqtSignal, Qt
from qgis.PyQt.QtGui import QColor, QCursor, QPixmap
import os


class PointCaptureTool(QgsMapTool):
    """Map tool that emits a signal when the user clicks on the canvas."""
    
    pointCaptured = pyqtSignal(QgsPointXY)

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self._marker = None

    def canvasPressEvent(self, event):
        if event.button() == Qt.LeftButton:
            point = self.toMapCoordinates(event.pos())
            self._draw_marker(point)
            self.pointCaptured.emit(point)

    def _draw_marker(self, point):
        if self._marker:
            self.canvas.scene().removeItem(self._marker)
        self._marker = QgsVertexMarker(self.canvas)
        self._marker.setCenter(point)
        self._marker.setColor(QColor(255, 0, 0))
        self._marker.setIconSize(12)
        self._marker.setIconType(QgsVertexMarker.ICON_CROSS)
        self._marker.setPenWidth(3)

    def clear_marker(self):
        if self._marker:
            self.canvas.scene().removeItem(self._marker)
            self._marker = None

    def deactivate(self):
        self.clear_marker()
        super().deactivate()
