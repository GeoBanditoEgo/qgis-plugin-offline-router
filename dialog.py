# -*- coding: utf-8 -*-
"""
RouterDialog — multi-waypoint routing with turn-by-turn directions.
"""
import os

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QGroupBox, QLineEdit, QFileDialog, QProgressBar,
    QMessageBox, QFrame, QCheckBox, QListWidget, QListWidgetItem,
    QSplitter, QTextEdit, QWidget, QSizePolicy, QAbstractItemView,
    QRadioButton, QButtonGroup
)
from qgis.PyQt.QtCore import Qt, QThread, pyqtSignal, QSettings, QPointF, QUrl
from qgis.PyQt.QtGui import QColor, QFont, QIcon, QDesktopServices

from qgis.core import (
    QgsProject, QgsVectorLayer, QgsFeature, QgsGeometry,
    QgsPointXY, QgsCoordinateReferenceSystem,
    QgsCoordinateTransform, QgsField,
    QgsSingleSymbolRenderer, QgsLineSymbol, QgsMarkerSymbol,
    QgsRuleBasedRenderer, QgsPalLayerSettings, QgsVectorLayerSimpleLabeling,
    QgsTextFormat, QgsTextBufferSettings, QgsUnitTypes,
    QgsLayerTreeGroup, QgsMarkerLineSymbolLayer, QgsFontMarkerSymbolLayer,
)
from PyQt5.QtCore import QVariant
from PyQt5.QtGui import QColor as PyQColor

from .map_tool import PointCaptureTool
from .routing import (run_multi_leg_route, get_spatialite_status,
                      probe_db, _fmt_dist, _fmt_duration, load_nodes_layer)
from .advanced_setup import AdvancedSetupDialog


# ---------------------------------------------------------------------------
# Worker thread
# ---------------------------------------------------------------------------
class RoutingWorker(QThread):
    finished = pyqtSignal(dict)
    error    = pyqtSignal(str)

    def __init__(self, db_path, waypoints_4326, imperial=False):
        super().__init__()
        self.db_path   = db_path
        self.waypoints = waypoints_4326   # [(lon,lat), ...]
        self.imperial  = imperial

    def run(self):
        try:
            result = run_multi_leg_route(self.db_path, self.waypoints,
                                         imperial=self.imperial)
            self.finished.emit(result)
        except Exception as e:
            self.error.emit(str(e))


# ---------------------------------------------------------------------------
# Main dialog
# ---------------------------------------------------------------------------
class RouterDialog(QDialog):
    SETTINGS_KEY_DB   = 'OfflineRouter/last_db_path'
    SETTINGS_KEY_UNIT = 'OfflineRouter/imperial_units'
    LAYER_ROUTE      = 'Route'
    LAYER_WAYPOINTS  = 'Route Waypoints'
    LAYER_NODES      = 'Road Routing Nodes'
    GROUP_NAME       = 'Routing (Temporary Layers)'

    def __init__(self, iface):
        super().__init__(iface.mainWindow())
        self.iface   = iface
        self.canvas  = iface.mapCanvas()

        # Read version from metadata.txt so the title stays in sync automatically
        _version = '1.0'
        try:
            _meta = os.path.join(os.path.dirname(__file__), 'metadata.txt')
            with open(_meta, 'r', encoding='utf-8') as _f:
                for _line in _f:
                    if _line.startswith('version='):
                        _version = _line.split('=', 1)[1].strip()
                        break
        except Exception:
            pass
        self.setWindowTitle(f'Offline Router v{_version} by GeoBanditoEgo')
        self.setMinimumWidth(520)
        self.setMinimumHeight(600)
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)

        self._map_tool   = None
        self._prev_tool  = None
        self._worker     = None
        self._adv_dialog = None
        self.plugin_dir  = os.path.dirname(__file__)
        self._preview_layer = None   # live waypoint preview on canvas
        # Waypoints: list of {'label': str, 'pt_3857': QgsPointXY, 'lat': float, 'lon': float}
        self._waypoints  = []

        crs_3857 = QgsCoordinateReferenceSystem('EPSG:3857')
        crs_4326 = QgsCoordinateReferenceSystem('EPSG:4326')
        project  = QgsProject.instance()

        # Project CRS is guaranteed to be 4326 or 3857 (plugin.py checks on open)
        self._project_crs = project.crs()
        self._is_4326 = (self._project_crs.authid() == 'EPSG:4326')

        # Transforms from project CRS → 4326/3857
        self._xform_to_4326 = QgsCoordinateTransform(self._project_crs, crs_4326, project)
        self._xform_to_3857 = QgsCoordinateTransform(self._project_crs, crs_3857, project)
        # Transform 4326 → 3857 (used when reloading waypoints from layers)
        self._xform_4326_to_3857 = QgsCoordinateTransform(crs_4326, crs_3857, project)

        self._build_ui()
        self._load_settings()
        self._update_status()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(6)

        # --- DB ---
        db_grp = QGroupBox('SpatiaLite Routing Database')
        db_row = QHBoxLayout(db_grp)
        self.db_edit = QLineEdit()
        self.db_edit.setPlaceholderText('Path to .sqlite routing file…')
        self.db_edit.textChanged.connect(self._update_status)
        browse_btn = QPushButton('Browse…')
        browse_btn.clicked.connect(self._browse_db)
        adv_icon_path = os.path.join(
            os.path.dirname(__file__), 'icons', 'advanced_setup.png')
        from qgis.PyQt.QtGui import QIcon as _QIcon
        adv_icon = _QIcon(adv_icon_path) if os.path.exists(adv_icon_path) else _QIcon()
        self.adv_btn = QPushButton(adv_icon, ' Build Routing File')
        self.adv_btn.setToolTip(
            'Build a routing database from a source PBF file and a GPX boundary')
        self.adv_btn.clicked.connect(self._open_advanced_setup)
        db_row.addWidget(self.db_edit)
        db_row.addWidget(browse_btn)
        db_row.addWidget(self.adv_btn)
        root.addWidget(db_grp)

        # --- Waypoints ---
        wp_grp = QGroupBox('Waypoints  (click Add, then click on map  |  drag rows to reorder)')
        wp_layout = QVBoxLayout(wp_grp)

        # List + up/down buttons side by side
        wp_list_row = QHBoxLayout()

        self.wp_list = QListWidget()
        self.wp_list.setDragDropMode(QAbstractItemView.InternalMove)
        self.wp_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.wp_list.setMinimumHeight(100)
        self.wp_list.model().rowsMoved.connect(self._on_list_reordered)
        self.wp_list.itemDoubleClicked.connect(self._on_wp_double_clicked)
        wp_list_row.addWidget(self.wp_list)

        # Up / Down arrow buttons stacked vertically beside the list
        ud_col = QVBoxLayout()
        ud_col.setSpacing(2)
        self.wp_up_btn = QPushButton('▲')
        self.wp_up_btn.setFixedWidth(32)
        self.wp_up_btn.setToolTip('Move selected waypoint up')
        self.wp_up_btn.clicked.connect(self._move_wp_up)
        self.wp_dn_btn = QPushButton('▼')
        self.wp_dn_btn.setFixedWidth(32)
        self.wp_dn_btn.setToolTip('Move selected waypoint down')
        self.wp_dn_btn.clicked.connect(self._move_wp_down)
        ud_col.addStretch()
        ud_col.addWidget(self.wp_up_btn)
        ud_col.addWidget(self.wp_dn_btn)
        ud_col.addStretch()
        wp_list_row.addLayout(ud_col)
        wp_layout.addLayout(wp_list_row)

        wp_btn_row = QHBoxLayout()
        self.add_wp_btn = QPushButton('➕ Add Waypoint')
        self.add_wp_btn.setCheckable(True)
        self.add_wp_btn.clicked.connect(self._activate_picker)
        self.del_wp_btn = QPushButton('🗑 Remove Selected')
        self.del_wp_btn.clicked.connect(self._remove_selected_waypoint)
        self.clear_wp_btn = QPushButton('Clear All')
        self.clear_wp_btn.clicked.connect(self._clear_waypoints)
        wp_btn_row.addWidget(self.add_wp_btn)
        wp_btn_row.addWidget(self.del_wp_btn)
        wp_btn_row.addWidget(self.clear_wp_btn)
        wp_layout.addLayout(wp_btn_row)
        root.addWidget(wp_grp)

        # --- Options ---
        opt_grp = QGroupBox('Options')
        opt_layout = QVBoxLayout(opt_grp)
        self.clear_prev_cb = QCheckBox('Remove previous route layers before adding new')
        self.clear_prev_cb.setChecked(True)
        opt_layout.addWidget(self.clear_prev_cb)

        # Unit system toggle
        unit_row = QHBoxLayout()
        unit_label = QLabel('Distance units:')
        self.unit_metric_rb   = QRadioButton('Metric  (m / km)')
        self.unit_imperial_rb = QRadioButton('Imperial  (ft / mi)')
        self.unit_metric_rb.setChecked(True)
        self._unit_group = QButtonGroup(self)
        self._unit_group.addButton(self.unit_metric_rb,   0)
        self._unit_group.addButton(self.unit_imperial_rb, 1)
        unit_row.addWidget(unit_label)
        unit_row.addWidget(self.unit_metric_rb)
        unit_row.addWidget(self.unit_imperial_rb)
        unit_row.addStretch()
        opt_layout.addLayout(unit_row)
        root.addWidget(opt_grp)

        # --- Status ---
        sep = QFrame(); sep.setFrameShape(QFrame.HLine)
        root.addWidget(sep)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        small = self.status_label.font()
        small.setPointSize(small.pointSize() - 1)
        self.status_label.setFont(small)
        root.addWidget(self.status_label)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setVisible(False)
        root.addWidget(self.progress)

        # --- Action buttons ---
        btn_row = QHBoxLayout()
        self.route_btn = QPushButton('▶  Find Route')
        self.route_btn.setEnabled(False)
        bold = QFont(); bold.setBold(True)
        self.route_btn.setFont(bold)
        self.route_btn.clicked.connect(self._run_routing)
        self.reload_btn = QPushButton('🔄 Reload Waypoints')
        self.reload_btn.setToolTip('Reload waypoints from an existing "Route Waypoints" layer in the project')
        self.reload_btn.clicked.connect(self._reload_waypoints_from_layer)
        close_btn = QPushButton('Close')
        close_btn.clicked.connect(self.close)
        btn_row.addWidget(self.route_btn)
        btn_row.addWidget(self.reload_btn)
        btn_row.addWidget(close_btn)
        root.addLayout(btn_row)

        # --- Directions panel ---
        dir_grp = QGroupBox('Turn-by-Turn Directions')
        dir_layout = QVBoxLayout(dir_grp)

        # Summary row
        self.summary_label = QLabel('—')
        self.summary_label.setWordWrap(True)
        sf = self.summary_label.font(); sf.setBold(True)
        self.summary_label.setFont(sf)
        dir_layout.addWidget(self.summary_label)

        self.directions_box = QTextEdit()
        self.directions_box.setReadOnly(True)
        self.directions_box.setMinimumHeight(160)
        self.directions_box.setFont(QFont('Courier', 9))
        dir_layout.addWidget(self.directions_box)
        root.addWidget(dir_grp)

        # --- Bottom bar: SpatiaLite status + User Guide button ---
        bottom_row = QHBoxLayout()

        hint = QLabel(get_spatialite_status())
        hint.setStyleSheet('color:#555; font-style:italic;')
        hint.setFont(small)
        bottom_row.addWidget(hint, 1)

        guide_btn = QPushButton('📖 User Guide')
        guide_btn.setToolTip('Open the Offline Router PDF user guide')
        guide_btn.setFixedWidth(120)
        guide_btn.clicked.connect(self._open_user_guide)
        bottom_row.addWidget(guide_btn)

        root.addLayout(bottom_row)

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    def _load_settings(self):
        s = QSettings()
        v = s.value(self.SETTINGS_KEY_DB, '')
        if v: self.db_edit.setText(v)
        imperial = s.value(self.SETTINGS_KEY_UNIT, False, type=bool)
        self.unit_imperial_rb.setChecked(imperial)
        self.unit_metric_rb.setChecked(not imperial)

    def _save_settings(self):
        s = QSettings()
        s.setValue(self.SETTINGS_KEY_DB, self.db_edit.text().strip())
        s.setValue(self.SETTINGS_KEY_UNIT, self.unit_imperial_rb.isChecked())

    @property
    def _imperial(self):
        return self.unit_imperial_rb.isChecked()

    # ------------------------------------------------------------------
    # DB browsing
    # ------------------------------------------------------------------
    def _browse_db(self):
        path, _ = QFileDialog.getOpenFileName(
            self, 'Select SpatiaLite Routing Database',
            self.db_edit.text() or '',
            'SpatiaLite databases (*.sqlite *.db *.spatialite);;All files (*)')
        if path:
            self.db_edit.setText(path)
            self._save_settings()
            self._load_nodes_layer(path)

    def _open_advanced_setup(self):
        """Open the Create Routing File dialog for building a routing database."""
        if self._adv_dialog is None:
            self._adv_dialog = AdvancedSetupDialog(parent=self)
            self._adv_dialog.buildSucceeded.connect(self._on_build_succeeded)
            self._adv_dialog.finished.connect(self._on_adv_dialog_closed)
        self._adv_dialog.show()
        self._adv_dialog.raise_()
        self._adv_dialog.activateWindow()

    def _on_build_succeeded(self, sqlite_path):
        """Auto-populate the DB path when a build finishes successfully."""
        self.db_edit.setText(sqlite_path)
        self._save_settings()
        self._load_nodes_layer(sqlite_path)

    def _on_adv_dialog_closed(self):
        self._adv_dialog = None

    def _load_nodes_layer(self, db_path):
        """Load road_routing_nodes into the map under the routing group."""
        project = QgsProject.instance()
        for lyr in project.mapLayersByName(self.LAYER_NODES):
            project.removeMapLayer(lyr.id())
        try:
            nodes_lyr = load_nodes_layer(db_path)
            if nodes_lyr:
                self._add_to_routing_group(nodes_lyr)
        except Exception as e:
            self._set_status(f'Note: Could not load nodes layer: {e}', '#888')

    def _get_or_create_routing_group(self):
        """Return the 'Routing (Temporary Layers)' layer tree group, creating it if needed."""
        root = QgsProject.instance().layerTreeRoot()
        group = root.findGroup(self.GROUP_NAME)
        if group is None:
            group = root.insertGroup(0, self.GROUP_NAME)
        return group

    def _add_to_routing_group(self, layer, at_top=False):
        """Add a layer to the project and insert it into the routing group.
        at_top=True inserts at position 0 (topmost in the panel)."""
        project = QgsProject.instance()
        project.addMapLayer(layer, False)   # False = don't add to tree yet
        group = self._get_or_create_routing_group()
        if at_top:
            group.insertLayer(0, layer)
        else:
            group.addLayer(layer)

    # ------------------------------------------------------------------
    # Waypoint management
    # ------------------------------------------------------------------
    def _activate_picker(self):
        if not self.add_wp_btn.isChecked():
            self._deactivate_picker()
            return
        self._map_tool = PointCaptureTool(self.canvas)
        self._map_tool.pointCaptured.connect(self._on_point_captured)
        self._prev_tool = self.canvas.mapTool()
        self.canvas.setMapTool(self._map_tool)
        self._set_status('Click on the map to add a waypoint…', 'blue')

    def _deactivate_picker(self):
        if self._map_tool:
            try: self._map_tool.pointCaptured.disconnect()
            except Exception: pass
            if self._prev_tool:
                self.canvas.setMapTool(self._prev_tool)
            else:
                self.canvas.unsetMapTool(self._map_tool)
            self._map_tool = None
        self.add_wp_btn.setChecked(False)

    def _on_point_captured(self, pt_project):
        # pt_project is in the project CRS (either 4326 or 3857)
        pt4326  = self._xform_to_4326.transform(pt_project)
        pt_3857 = self._xform_to_3857.transform(pt_project)
        idx = len(self._waypoints)
        if idx == 0:
            label = 'Start'
        else:
            label = f'Via {idx}'
        self._waypoints.append({
            'label':   label,
            'pt_3857': pt_3857,
            'lon':     pt4326.x(),
            'lat':     pt4326.y(),
        })
        self._refresh_wp_list()
        self._update_preview_layer()
        # Stay in picker mode so user can keep clicking waypoints
        self._update_status()

    def _refresh_wp_list(self):
        self.wp_list.clear()
        n = len(self._waypoints)
        via_counter = 0
        for i, wp in enumerate(self._waypoints):
            last = (i == n - 1)
            if i == 0:
                wp['label'] = 'Start'
                icon = '🟢'
            elif last and n > 1:
                wp['label'] = 'End'
                icon = '🔴'
            else:
                via_counter += 1
                wp['label'] = f'Via {via_counter}'
                icon = '🔵'
            item = QListWidgetItem(
                f'{icon}  {wp["label"]}  —  {wp["lat"]:.5f}°N  {wp["lon"]:.5f}°E'
            )
            self.wp_list.addItem(item)

    def _remove_selected_waypoint(self):
        row = self.wp_list.currentRow()
        if row < 0: return
        self._waypoints.pop(row)
        self._refresh_wp_list()
        self._update_preview_layer()
        self._update_status()

    def _clear_waypoints(self):
        self._deactivate_picker()
        self._waypoints.clear()
        self.wp_list.clear()
        self.summary_label.setText('—')
        self.directions_box.clear()
        self._update_preview_layer()
        self._update_status()

    def _update_preview_layer(self):
        """
        Maintain a live 'Route Waypoints (Preview)' memory layer that shows
        all currently-set waypoint pins on the map as they are added/removed.
        The layer is removed when waypoints are cleared or a final route is run.
        """
        from qgis.core import (QgsVectorLayer, QgsFeature, QgsGeometry,
                                QgsField, QgsProject, QgsRuleBasedRenderer,
                                QgsMarkerSymbol)
        from PyQt5.QtCore import QVariant

        project = QgsProject.instance()
        PREVIEW_NAME = 'Route Waypoints (Preview)'

        # Remove existing preview layer
        for lyr in project.mapLayersByName(PREVIEW_NAME):
            project.removeMapLayer(lyr.id())
        self._preview_layer = None

        if not self._waypoints:
            return

        lyr = QgsVectorLayer('Point?crs=EPSG:3857', PREVIEW_NAME, 'memory')
        dp  = lyr.dataProvider()
        dp.addAttributes([
            QgsField('label', QVariant.String),
            QgsField('type',  QVariant.String),
        ])
        lyr.updateFields()

        n = len(self._waypoints)
        for i, wp in enumerate(self._waypoints):
            f = QgsFeature(lyr.fields())
            f.setGeometry(QgsGeometry.fromPointXY(wp['pt_3857']))
            f['label'] = wp['label']
            f['type']  = 'start' if i == 0 else ('end' if i == n - 1 else 'via')
            dp.addFeature(f)
        lyr.updateExtents()

        # Styled to match the final waypoints layer colours
        sym_start = QgsMarkerSymbol.createSimple({
            'name': 'circle', 'color': '#00cc44', 'size': '4',
            'size_unit': 'MM', 'outline_color': '#005522', 'outline_width': '0.5'})
        sym_end = QgsMarkerSymbol.createSimple({
            'name': 'circle', 'color': '#ee1100', 'size': '4',
            'size_unit': 'MM', 'outline_color': '#660000', 'outline_width': '0.5'})
        sym_via = QgsMarkerSymbol.createSimple({
            'name': 'circle', 'color': '#0088ff', 'size': '3',
            'size_unit': 'MM', 'outline_color': '#003388', 'outline_width': '0.4'})
        root = QgsRuleBasedRenderer.Rule(None)
        for sym, expr, lbl in [
            (sym_start, '"type"=\'start\'', 'Start'),
            (sym_end,   '"type"=\'end\'',   'End'),
            (sym_via,   '"type"=\'via\'',   'Via'),
        ]:
            r = QgsRuleBasedRenderer.Rule(sym)
            r.setFilterExpression(expr)
            r.setLabel(lbl)
            root.appendChild(r)
        lyr.setRenderer(QgsRuleBasedRenderer(root))

        self._add_to_routing_group(lyr, at_top=True)
        self._preview_layer = lyr
        lyr.triggerRepaint()

    def _on_list_reordered(self, parent, start, end, dest, dest_row):
        """Sync _waypoints to the new row order after a drag-and-drop move."""
        # Take the moved item out and reinsert at the drop position
        moved = self._waypoints.pop(start)
        insert_at = dest_row if dest_row <= start else dest_row - 1
        self._waypoints.insert(insert_at, moved)
        self._refresh_wp_list()
        # Restore selection to the moved row
        self.wp_list.setCurrentRow(insert_at)
        self._update_preview_layer()
        self._update_status()

    def _move_wp_up(self):
        """Move the selected waypoint one position up."""
        row = self.wp_list.currentRow()
        if row <= 0 or row >= len(self._waypoints):
            return
        self._waypoints.insert(row - 1, self._waypoints.pop(row))
        self._refresh_wp_list()
        self.wp_list.setCurrentRow(row - 1)
        self._update_preview_layer()
        self._update_status()

    def _move_wp_down(self):
        """Move the selected waypoint one position down."""
        row = self.wp_list.currentRow()
        if row < 0 or row >= len(self._waypoints) - 1:
            return
        self._waypoints.insert(row + 1, self._waypoints.pop(row))
        self._refresh_wp_list()
        self.wp_list.setCurrentRow(row + 1)
        self._update_preview_layer()
        self._update_status()

    def _on_wp_double_clicked(self, item):
        """Re-center the map on the double-clicked waypoint."""
        row = self.wp_list.row(item)
        if row < 0 or row >= len(self._waypoints):
            return
        wp = self._waypoints[row]
        # Pan using a point in the project CRS: stored as 4326 lon/lat, so
        # convert to 3857 if project is 3857, or use lon/lat directly if 4326.
        if self._is_4326:
            from qgis.core import QgsPointXY as _QPXY
            pt = _QPXY(wp['lon'], wp['lat'])
        else:
            pt = wp['pt_3857']
        extent = self.canvas.extent()
        half_w = extent.width() / 2
        half_h = extent.height() / 2
        from qgis.core import QgsRectangle
        new_extent = QgsRectangle(
            pt.x() - half_w, pt.y() - half_h,
            pt.x() + half_w, pt.y() + half_h,
        )
        self.canvas.setExtent(new_extent)
        self.canvas.refresh()

    # ------------------------------------------------------------------
    # Status / validation
    # ------------------------------------------------------------------
    def _update_status(self):
        db = self.db_edit.text().strip()
        msgs = []; ready = True

        if not db:
            msgs.append('• No database selected.'); ready = False
        elif not os.path.isfile(db):
            msgs.append('• Database file not found.'); ready = False
        else:
            info = probe_db(db)
            if not info['ok']:
                msgs.append(f'• DB: {info["message"]}'); ready = False

        n = len(self._waypoints)
        if n < 2:
            msgs.append(f'• Need at least 2 waypoints ({n} set).'); ready = False

        if ready:
            legs = n - 1
            self._set_status(
                f'Ready — {n} waypoints, {legs} leg{"s" if legs!=1 else ""}. '
                'Click "Find Route".', 'green')
        else:
            self._set_status('\n'.join(msgs), '#888')

        self.route_btn.setEnabled(ready)

    def _set_status(self, text, color='black'):
        self.status_label.setText(text)
        self.status_label.setStyleSheet(f'color:{color};')

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------
    def _run_routing(self):
        db = self.db_edit.text().strip()
        self._save_settings()
        waypoints_4326 = [(wp['lon'], wp['lat']) for wp in self._waypoints]

        self.route_btn.setEnabled(False)
        self.progress.setVisible(True)
        self._set_status('Routing…', 'blue')
        self.directions_box.clear()
        self.summary_label.setText('—')

        self._worker = RoutingWorker(db, waypoints_4326, imperial=self._imperial)
        self._worker.finished.connect(self._on_routing_finished)
        self._worker.error.connect(self._on_routing_error)
        self._worker.start()

    def _on_routing_finished(self, result):
        self.progress.setVisible(False)
        total_m    = result['total_m']
        total_cost = result['total_cost']
        directions = result['directions']
        n_legs = len(self._waypoints) - 1

        # Summary
        imperial   = result.get('imperial', False)
        dist_str   = _fmt_dist(total_m, imperial=imperial)
        time_str   = _fmt_duration(total_cost) if total_cost > 0 else '—'
        unit_label = 'ft/mi' if imperial else 'm/km'
        self.summary_label.setText(
            f'📍 {len(self._waypoints)} waypoints · {n_legs} leg{"s" if n_legs!=1 else ""}  '
            f'|  📏 {dist_str}  |  ⏱ {time_str}  |  [{unit_label}]'
        )
        self._set_status(f'Route found — {dist_str}, approx {time_str}', 'green')

        # Directions text
        self._render_directions(directions, total_m, total_cost,
                                imperial=result.get('imperial', False))

        try:
            self._load_result_to_map(result)
        except Exception as e:
            QMessageBox.critical(self, 'Map Error', str(e))

        self.route_btn.setEnabled(True)

    def _on_routing_error(self, message):
        self.progress.setVisible(False)
        self.route_btn.setEnabled(True)
        self._set_status('Routing failed.', 'red')
        QMessageBox.critical(self, 'Routing Error', message)

    def _render_directions(self, directions, total_m, total_cost, imperial=False):
        lines = []
        for step in directions:
            num   = f'{step["step"]:>3}.'
            instr = step['instruction']
            dist  = step['distance_str']          # already formatted by routing engine
            cum   = _fmt_dist(step['cumulative_m'], imperial=imperial)

            if step['is_waypoint'] and step['waypoint_label']:
                lines.append('')
                lines.append(f'── {step["waypoint_label"]} ──────────────────────────')
                lines.append(f'     {instr}')
                lines.append('')
            else:
                if dist:
                    lines.append(f'{num} {instr}')
                    lines.append(f'       ↳ {dist}  (at {cum})')
                else:
                    lines.append(f'{num} {instr}')

        self.directions_box.setPlainText('\n'.join(lines))

    # ------------------------------------------------------------------
    # Map loading
    # ------------------------------------------------------------------
    def _load_result_to_map(self, result):
        project  = QgsProject.instance()
        imperial = result.get('imperial', False)
        crs_4326 = QgsCoordinateReferenceSystem('EPSG:4326')
        crs_3857 = QgsCoordinateReferenceSystem('EPSG:3857')
        xf = QgsCoordinateTransform(crs_4326, crs_3857, project)

        if self.clear_prev_cb.isChecked():
            for name in (self.LAYER_ROUTE, self.LAYER_WAYPOINTS, self.LAYER_NODES):
                for lyr in project.mapLayersByName(name):
                    project.removeMapLayer(lyr.id())

        # Remove the live preview layer — it is replaced by the final waypoints layer
        for lyr in project.mapLayersByName('Route Waypoints (Preview)'):
            project.removeMapLayer(lyr.id())
        self._preview_layer = None

        # ---- Collect and transform route geometry segments ----
        all_geoms = []
        for seg in result['all_segments']:
            wkt = seg.get('geometry_wkt') or ''
            if not wkt: continue
            geom = QgsGeometry.fromWkt(wkt)
            if geom.isNull(): continue
            parts = geom.asGeometryCollection() if geom.isMultipart() else [geom]
            for p in parts:
                p.transform(xf)
                all_geoms.append(p)

        if not all_geoms:
            raise RuntimeError('No geometry returned from routing engine.')

        merged = QgsGeometry.collectGeometry(all_geoms)

        # ---- Route line layer ----
        line_layer = QgsVectorLayer('MultiLineString?crs=EPSG:3857', self.LAYER_ROUTE, 'memory')
        dp = line_layer.dataProvider()
        dp.addAttributes([
            QgsField('distance_m',      QVariant.Double),
            QgsField('distance',        QVariant.String),
            QgsField('travel_duration', QVariant.String),
            QgsField('waypoints',       QVariant.Int),
            QgsField('directions_text', QVariant.String),
        ])
        line_layer.updateFields()

        # Build plain-text directions string for attribute
        dir_lines = []
        for step in result.get('directions', []):
            if step['is_waypoint'] and step['waypoint_label']:
                dir_lines.append(f'── {step["waypoint_label"]} ──')
                dir_lines.append(f'  {step["instruction"]}')
            else:
                num = f'{step["step"]:>3}.'
                dist = step['distance_str']
                cum  = _fmt_dist(step['cumulative_m'], imperial=imperial)
                if dist:
                    dir_lines.append(f'{num} {step["instruction"]}')
                    dir_lines.append(f'     ↳ {dist}  (at {cum})')
                else:
                    dir_lines.append(f'{num} {step["instruction"]}')
        directions_text = '\n'.join(dir_lines)

        feat = QgsFeature(line_layer.fields())
        feat.setGeometry(merged)
        feat['distance_m']      = result['total_m']
        feat['distance']        = _fmt_dist(result['total_m'], imperial=imperial)
        feat['travel_duration'] = _fmt_duration(result['total_cost']) if result['total_cost'] > 0 else ''
        feat['waypoints']       = len(self._waypoints)
        feat['directions_text'] = directions_text
        dp.addFeature(feat)
        line_layer.updateExtents()

        # Apply QML style; fall back to a plain purple line if file missing
        qml_route = os.path.join(self.plugin_dir, 'styles', 'Route-Arrow-Style.qml')
        if os.path.isfile(qml_route):
            line_layer.loadNamedStyle(qml_route)
        else:
            symbol = QgsLineSymbol.createSimple({
                'color': '#7B2FBE', 'width': '1.4',
                'capstyle': 'round', 'joinstyle': 'round'})
            line_layer.setRenderer(QgsSingleSymbolRenderer(symbol))

        self._add_to_routing_group(line_layer)           # Route sits below waypoints

        # ---- Waypoints layer ----
        # Inserted at top of group so it renders above the route line
        pt_layer = QgsVectorLayer('Point?crs=EPSG:3857', self.LAYER_WAYPOINTS, 'memory')
        dp2 = pt_layer.dataProvider()
        dp2.addAttributes([
            QgsField('label', QVariant.String),
            QgsField('type',  QVariant.String),
        ])
        pt_layer.updateFields()

        n = len(self._waypoints)
        for i, wp in enumerate(self._waypoints):
            f = QgsFeature(pt_layer.fields())
            f.setGeometry(QgsGeometry.fromPointXY(wp['pt_3857']))
            f['label'] = wp['label']
            f['type']  = 'start' if i == 0 else ('end' if i == n-1 else 'via')
            dp2.addFeature(f)
        pt_layer.updateExtents()

        # Apply QML style; fall back to coded rule-based symbols if file missing
        qml_wp = os.path.join(self.plugin_dir, 'styles', 'Route-Waypoints-Style.qml')
        if os.path.isfile(qml_wp):
            pt_layer.loadNamedStyle(qml_wp)
        else:
            sym_start = QgsMarkerSymbol.createSimple({
                'name': 'equilateral_triangle', 'color': '#00cc44',
                'size': '5', 'size_unit': 'MM',
                'outline_color': '#005522', 'outline_width': '0.5',
                'outline_width_unit': 'MM'})
            sym_end = QgsMarkerSymbol.createSimple({
                'name': 'equilateral_triangle', 'color': '#ee1100',
                'size': '5', 'size_unit': 'MM',
                'outline_color': '#660000', 'outline_width': '0.5',
                'outline_width_unit': 'MM'})
            sym_via = QgsMarkerSymbol.createSimple({
                'name': 'diamond', 'color': '#0088ff',
                'size': '3.5', 'size_unit': 'MM',
                'outline_color': '#003388', 'outline_width': '0.4',
                'outline_width_unit': 'MM'})
            root = QgsRuleBasedRenderer.Rule(None)
            for sym, expr, lbl in [
                (sym_start, '"type"=\'start\'', 'Start'),
                (sym_end,   '"type"=\'end\'',   'End'),
                (sym_via,   '"type"=\'via\'',   'Via'),
            ]:
                r = QgsRuleBasedRenderer.Rule(sym)
                r.setFilterExpression(expr); r.setLabel(lbl)
                root.appendChild(r)
            pt_layer.setRenderer(QgsRuleBasedRenderer(root))

        # Labels (always applied regardless of QML)
        pal = QgsPalLayerSettings()
        pal.fieldName = 'label'
        pal.isExpression = False
        pal.enabled = True
        font = QFont('Open Sans')
        font.setStyleHint(QFont.SansSerif)
        tf = QgsTextFormat()
        tf.setFont(font)
        tf.setSize(12)
        tf.setColor(PyQColor(0, 0, 0))
        buf = QgsTextBufferSettings()
        buf.setEnabled(True)
        buf.setSize(1.5)
        buf.setColor(PyQColor(255, 255, 255))
        tf.setBuffer(buf)
        pal.setFormat(tf)
        pal.placement = QgsPalLayerSettings.AroundPoint
        pal.dist = 2.0
        pal.distUnits = QgsUnitTypes.RenderMillimeters
        pt_layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
        pt_layer.setLabelsEnabled(True)

        self._add_to_routing_group(pt_layer, at_top=True)   # Waypoints always on top
        pt_layer.triggerRepaint()

        # Zoom to route
        ext = line_layer.extent()
        if self._is_4326:
            from qgis.core import QgsCoordinateTransform as _XF, QgsCoordinateReferenceSystem as _CRS
            xf_to_proj = _XF(_CRS('EPSG:3857'), _CRS('EPSG:4326'), QgsProject.instance())
            ext = xf_to_proj.transformBoundingBox(ext)
        margin = max(ext.width(), ext.height()) * 0.12
        ext.grow(margin)
        self.canvas.setExtent(ext)
        self.canvas.refresh()

    # ------------------------------------------------------------------
    # Reload waypoints from existing layer (Change 5)
    # ------------------------------------------------------------------
    def _reload_waypoints_from_layer(self):
        """Reload waypoints from an existing 'Route Waypoints' layer in the project."""
        project = QgsProject.instance()
        layers = project.mapLayersByName(self.LAYER_WAYPOINTS)
        if not layers:
            QMessageBox.information(
                self, 'Reload Waypoints',
                f'No layer named "{self.LAYER_WAYPOINTS}" found in the project.\n'
                'Run a route first to create one.')
            return

        lyr = layers[0]
        feats = list(lyr.getFeatures())
        # Sort features: Start first, then Via 1/2/..., then End
        def _sort_key(feat):
            lbl = (feat['label'] or '').strip()
            if lbl == 'Start':
                return (0, 0)
            if lbl == 'End':
                return (2, 0)
            # Via N
            try:
                return (1, int(lbl.split()[-1]))
            except (ValueError, IndexError):
                return (1, 0)
        feats.sort(key=_sort_key)

        if not feats:
            QMessageBox.information(self, 'Reload Waypoints', 'The waypoints layer has no features.')
            return

        self._waypoints.clear()
        crs_layer = lyr.crs()
        crs_3857  = QgsCoordinateReferenceSystem('EPSG:3857')
        crs_4326  = QgsCoordinateReferenceSystem('EPSG:4326')
        xf_to_3857 = QgsCoordinateTransform(crs_layer, crs_3857, project)
        xf_to_4326 = QgsCoordinateTransform(crs_layer, crs_4326, project)

        for f in feats:
            geom = f.geometry()
            if geom.isNull():
                continue
            pt_orig = geom.asPoint()
            pt_3857 = xf_to_3857.transform(QgsPointXY(pt_orig))
            pt_4326 = xf_to_4326.transform(QgsPointXY(pt_orig))
            try:
                label = f['label']
            except Exception:
                label = ''
            self._waypoints.append({
                'label':   label or f'Point {len(self._waypoints)+1}',
                'pt_3857': pt_3857,
                'lon':     pt_4326.x(),
                'lat':     pt_4326.y(),
            })

        self._refresh_wp_list()
        self._update_status()
        self._set_status(
            f'Reloaded {len(self._waypoints)} waypoint(s) from "{self.LAYER_WAYPOINTS}" layer.',
            'green')

    # ------------------------------------------------------------------
    # User Guide
    # ------------------------------------------------------------------
    def _open_user_guide(self):
        """Open the PDF user guide using the system default PDF viewer."""
        pdf_path = os.path.join(self.plugin_dir, 'userguide',
                                'Offline_Router_User_Guide.pdf')
        if not os.path.isfile(pdf_path):
            from qgis.PyQt.QtWidgets import QMessageBox
            QMessageBox.warning(
                self, 'User Guide Not Found',
                f'Could not find the user guide at:\n{pdf_path}')
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(pdf_path))

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------
    def closeEvent(self, event):
        self._deactivate_picker()
        if self._worker and self._worker.isRunning():
            self._worker.quit(); self._worker.wait(2000)
        if self._adv_dialog:
            self._adv_dialog.close()
        # Remove the preview layer if it's still on the canvas
        if self._preview_layer:
            try:
                QgsProject.instance().removeMapLayer(self._preview_layer.id())
            except Exception:
                pass
            self._preview_layer = None
        super().closeEvent(event)
