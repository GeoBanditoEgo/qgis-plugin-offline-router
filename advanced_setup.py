# -*- coding: utf-8 -*-
"""
AdvancedSetupDialog — Build a SpatiaLite routing database from a PBF source file.

Workflow:
  1. Convert the supplied GPX polygon track to an Osmosis .POLY filter file.
  2. Run osmconvert64.exe  -> clip the source PBF to the POLY boundary.
  3. Run spatialite_osm_net.exe -> import the clipped PBF into a raw routing DB.
  4. Run spatialite.exe         -> execute the bundled createrouting-by-car.sql.

NOTE: This feature uses Windows .exe tools and will only work on Windows.
"""

import os
import re
import tempfile
import subprocess

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGroupBox,
    QLabel, QLineEdit, QPushButton, QFileDialog,
    QTextEdit, QProgressBar, QMessageBox, QFrame,
)
from qgis.PyQt.QtCore import Qt, QThread, pyqtSignal, QSettings
from qgis.PyQt.QtGui import QFont


# ---------------------------------------------------------------------------
# Bundled SQL path — static, never changes
# ---------------------------------------------------------------------------
_SQL_PATH = os.path.join(
    os.path.dirname(__file__), 'tools', 'createrouting-by-car.sql'
)


def _tools_dir():
    return os.path.join(os.path.dirname(__file__), 'tools')


def _tool(name):
    return os.path.join(_tools_dir(), name)


def _gpx_track_to_poly(gpx_path, poly_path):
    """Parse a GPX polygon track and write an Osmosis .poly file."""
    with open(gpx_path, 'r', encoding='utf-8', errors='replace') as fh:
        content = fh.read()

    pattern = re.compile(
        r'<trkpt\s[^>]*lat=["\']([^"\']+)["\'][^>]*lon=["\']([^"\']+)["\']',
        re.IGNORECASE,
    )
    points = [(float(m.group(1)), float(m.group(2)))
              for m in pattern.finditer(content)]

    if not points:
        pattern2 = re.compile(
            r'<wpt\s[^>]*lat=["\']([^"\']+)["\'][^>]*lon=["\']([^"\']+)["\']',
            re.IGNORECASE,
        )
        points = [(float(m.group(1)), float(m.group(2)))
                  for m in pattern2.finditer(content)]

    if not points:
        raise ValueError(
            'No track points (trkpt) or waypoints (wpt) found in the GPX file.'
        )

    if points[0] != points[-1]:
        points.append(points[0])

    with open(poly_path, 'w', encoding='utf-8') as fh:
        fh.write('polygon\n')
        fh.write('1\n')
        for lat, lon in points:
            fh.write(f'   {lon:.7f}   {lat:.7f}\n')
        fh.write('END\n')
        fh.write('END\n')


# ---------------------------------------------------------------------------
# Worker thread
# ---------------------------------------------------------------------------

class BuildWorker(QThread):
    log      = pyqtSignal(str)
    progress = pyqtSignal(int)
    finished = pyqtSignal(bool, str)

    def __init__(self, pbf_path, gpx_path, output_sqlite, parent=None):
        super().__init__(parent)
        self.pbf_path      = pbf_path
        self.gpx_path      = gpx_path
        self.output_sqlite = output_sqlite

    def run(self):
        tmp_dir = tempfile.mkdtemp(prefix='offline_router_')
        try:
            self._run(tmp_dir)
        except Exception as exc:
            self.finished.emit(False, str(exc))
        finally:
            for name in ('clipped.osm.pbf', 'boundary.poly'):
                p = os.path.join(tmp_dir, name)
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
            try:
                os.rmdir(tmp_dir)
            except OSError:
                pass

    def _run(self, tmp_dir):
        poly_path        = os.path.join(tmp_dir, 'boundary.poly')
        clipped_pbf_path = os.path.join(tmp_dir, 'clipped.osm.pbf')

        self.log.emit('-- Step 1: Converting GPX track to POLY boundary file...')
        self.progress.emit(5)
        _gpx_track_to_poly(self.gpx_path, poly_path)
        self.log.emit(f'   POLY file written: {poly_path}')
        self.progress.emit(15)

        self.log.emit('')
        self.log.emit('-- Step 2: Clipping PBF with osmconvert64...')
        cmd2 = [
            _tool('osmconvert64.exe'),
            self.pbf_path,
            f'-B={poly_path}',
            f'-o={clipped_pbf_path}',
        ]
        self.log.emit('   ' + ' '.join(cmd2))
        self._exec(cmd2, 'osmconvert64')
        self.progress.emit(45)

        self.log.emit('')
        self.log.emit('-- Step 3: Importing road network into SpatiaLite...')
        cmd3 = [
            _tool('spatialite_osm_net.exe'),
            '-o', clipped_pbf_path,
            '-d', self.output_sqlite,
            '-T', 'road_routing',
            '-m',
        ]
        self.log.emit('   ' + ' '.join(cmd3))
        self._exec(cmd3, 'spatialite_osm_net')
        self.progress.emit(70)

        self.log.emit('')
        self.log.emit('-- Step 4: Running CreateRouting SQL...')
        cmd4 = [
            _tool('spatialite.exe'),
            self.output_sqlite,
        ]
        self.log.emit('   ' + ' '.join(cmd4) + f'  <  {_SQL_PATH}')
        with open(_SQL_PATH, 'r', encoding='utf-8') as fh:
            sql_content = fh.read()
        self._exec(cmd4, 'spatialite', stdin_text=sql_content)
        self.progress.emit(100)

        self.log.emit('')
        self.log.emit('Build complete!')
        self.log.emit(f'   Output: {self.output_sqlite}')
        self.finished.emit(True, self.output_sqlite)

    def _exec(self, cmd, label, stdin_text=None):
        kwargs = dict(
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='replace',
        )
        if stdin_text is not None:
            kwargs['stdin'] = subprocess.PIPE

        proc = subprocess.Popen(cmd, **kwargs)

        if stdin_text is not None:
            stdout_data, _ = proc.communicate(input=stdin_text)
            for line in stdout_data.splitlines():
                if line.strip():
                    self.log.emit(f'   [{label}] {line}')
        else:
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    self.log.emit(f'   [{label}] {line}')
            proc.wait()

        if proc.returncode not in (0, None):
            raise RuntimeError(
                f'{label} exited with code {proc.returncode}. '
                'Check the log for details.'
            )


# ---------------------------------------------------------------------------
# Dialog
# ---------------------------------------------------------------------------

class AdvancedSetupDialog(QDialog):

    SETTINGS_PBF    = 'OfflineRouter/adv_pbf_path'
    SETTINGS_GPX    = 'OfflineRouter/adv_gpx_path'
    SETTINGS_OUTPUT = 'OfflineRouter/adv_output_sqlite'

    buildSucceeded = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Create Routing File')
        self.setMinimumWidth(640)
        self.setMinimumHeight(520)
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        self._worker = None
        self._build_ui()
        self._load_settings()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)

        # Windows-only warning banner
        notice = QLabel(
            '<b>\u26a0\ufe0f  Windows only:</b>  This feature uses Windows .exe tools '
            '(osmconvert64, spatialite_osm_net, spatialite) and will '
            '<b>not work on macOS or Linux</b>.'
        )
        notice.setWordWrap(True)
        notice.setTextFormat(Qt.RichText)
        notice.setStyleSheet(
            'background:#fff3cd; color:#856404; border:1px solid #ffc107;'
            'border-radius:4px; padding:8px;'
        )
        root.addWidget(notice)

        # Source PBF
        pbf_grp = QGroupBox('Source PBF File')
        pbf_row = QHBoxLayout(pbf_grp)
        self.pbf_edit = QLineEdit()
        self.pbf_edit.setPlaceholderText('Path to source .osm.pbf file...')
        self.pbf_edit.textChanged.connect(self._validate)
        pbf_browse = QPushButton('Browse...')
        pbf_browse.clicked.connect(self._browse_pbf)
        pbf_row.addWidget(self.pbf_edit)
        pbf_row.addWidget(pbf_browse)
        root.addWidget(pbf_grp)

        # GPX boundary
        gpx_grp = QGroupBox('GPX Boundary File  (polygon track)')
        gpx_row = QHBoxLayout(gpx_grp)
        self.gpx_edit = QLineEdit()
        self.gpx_edit.setPlaceholderText('Path to .gpx file containing a polygon track...')
        self.gpx_edit.textChanged.connect(self._validate)
        gpx_browse = QPushButton('Browse...')
        gpx_browse.clicked.connect(self._browse_gpx)
        gpx_row.addWidget(self.gpx_edit)
        gpx_row.addWidget(gpx_browse)
        root.addWidget(gpx_grp)

        # Output SQLite
        out_grp = QGroupBox('Output Routing Database')
        out_row = QHBoxLayout(out_grp)
        self.out_edit = QLineEdit()
        self.out_edit.setPlaceholderText('Output path for routing .sqlite file...')
        self.out_edit.textChanged.connect(self._validate)
        out_browse = QPushButton('Browse...')
        out_browse.clicked.connect(self._browse_output)
        out_row.addWidget(self.out_edit)
        out_row.addWidget(out_browse)
        root.addWidget(out_grp)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        root.addWidget(sep)

        self.status_label = QLabel('')
        self.status_label.setWordWrap(True)
        small = self.status_label.font()
        small.setPointSize(small.pointSize() - 1)
        self.status_label.setFont(small)
        root.addWidget(self.status_label)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setVisible(False)
        root.addWidget(self.progress)

        log_grp = QGroupBox('Build Log')
        log_layout = QVBoxLayout(log_grp)
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setFont(QFont('Courier', 8))
        self.log_box.setMinimumHeight(160)
        log_layout.addWidget(self.log_box)
        root.addWidget(log_grp)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self.run_btn = QPushButton('Run')
        bold = QFont(); bold.setBold(True)
        self.run_btn.setFont(bold)
        self.run_btn.setEnabled(False)
        self.run_btn.clicked.connect(self._on_run)
        self.cancel_btn = QPushButton('Cancel')
        self.cancel_btn.clicked.connect(self._on_cancel)
        btn_row.addWidget(self.run_btn)
        btn_row.addWidget(self.cancel_btn)
        root.addLayout(btn_row)

    def _browse_pbf(self):
        path, _ = QFileDialog.getOpenFileName(
            self, 'Select Source PBF File', self.pbf_edit.text() or '',
            'OSM PBF files (*.pbf *.osm.pbf);;All files (*)')
        if path:
            self.pbf_edit.setText(path); self._save_settings()

    def _browse_gpx(self):
        path, _ = QFileDialog.getOpenFileName(
            self, 'Select GPX Boundary File', self.gpx_edit.text() or '',
            'GPX files (*.gpx);;All files (*)')
        if path:
            self.gpx_edit.setText(path); self._save_settings()

    def _browse_output(self):
        path, _ = QFileDialog.getSaveFileName(
            self, 'Save Routing Database As', self.out_edit.text() or '',
            'SpatiaLite databases (*.sqlite);;All files (*)')
        if path:
            if not path.lower().endswith('.sqlite'):
                path += '.sqlite'
            self.out_edit.setText(path); self._save_settings()

    def _load_settings(self):
        s = QSettings()
        self.pbf_edit.setText(s.value(self.SETTINGS_PBF, ''))
        self.gpx_edit.setText(s.value(self.SETTINGS_GPX, ''))
        self.out_edit.setText(s.value(self.SETTINGS_OUTPUT, ''))
        self._validate()

    def _save_settings(self):
        s = QSettings()
        s.setValue(self.SETTINGS_PBF,    self.pbf_edit.text().strip())
        s.setValue(self.SETTINGS_GPX,    self.gpx_edit.text().strip())
        s.setValue(self.SETTINGS_OUTPUT, self.out_edit.text().strip())

    def _validate(self):
        pbf = self.pbf_edit.text().strip()
        gpx = self.gpx_edit.text().strip()
        out = self.out_edit.text().strip()
        errors = []
        if not pbf:
            errors.append('Source PBF file is required.')
        elif not os.path.isfile(pbf):
            errors.append('Source PBF file not found.')
        if not gpx:
            errors.append('GPX boundary file is required.')
        elif not os.path.isfile(gpx):
            errors.append('GPX boundary file not found.')
        if not out:
            errors.append('Output SQLite path is required.')
        if not os.path.isfile(_SQL_PATH):
            errors.append(f'Bundled SQL script missing: {_SQL_PATH}')
        for exe in ('osmconvert64.exe', 'spatialite_osm_net.exe', 'spatialite.exe'):
            if not os.path.isfile(_tool(exe)):
                errors.append(f'Tool not found: tools/{exe}')
        if errors:
            self._set_status('\n'.join('• ' + e for e in errors), '#888')
            self.run_btn.setEnabled(False)
        else:
            self._set_status('Ready to build.', 'green')
            self.run_btn.setEnabled(True)

    def _set_status(self, text, color='black'):
        self.status_label.setText(text)
        self.status_label.setStyleSheet(f'color:{color};')

    def _on_run(self):
        pbf = self.pbf_edit.text().strip()
        gpx = self.gpx_edit.text().strip()
        out = self.out_edit.text().strip()
        if os.path.exists(out):
            reply = QMessageBox.question(
                self, 'File Exists',
                f'The output file already exists:\n{out}\n\nOverwrite it?',
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply != QMessageBox.Yes:
                return
            try:
                os.remove(out)
            except OSError as e:
                QMessageBox.critical(self, 'Error', f'Could not remove existing file:\n{e}')
                return
        self._save_settings()
        self.log_box.clear()
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.run_btn.setEnabled(False)
        self._set_status('Building...', 'blue')
        self._worker = BuildWorker(pbf, gpx, out, parent=self)
        self._worker.log.connect(self._append_log)
        self._worker.progress.connect(self.progress.setValue)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

    def _on_cancel(self):
        if self._worker and self._worker.isRunning():
            reply = QMessageBox.question(
                self, 'Cancel Build', 'A build is in progress. Terminate it?',
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply == QMessageBox.Yes:
                self._worker.terminate()
                self._worker.wait(3000)
                self._append_log('Build cancelled by user.')
                self.progress.setVisible(False)
                self.run_btn.setEnabled(True)
                self._set_status('Cancelled.', '#888')
        else:
            self.close()

    def _append_log(self, line):
        self.log_box.append(line)
        sb = self.log_box.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _on_finished(self, success, message):
        self.progress.setVisible(False)
        self.run_btn.setEnabled(True)
        if success:
            self._set_status('Build complete!', 'green')
            self.buildSucceeded.emit(message)
            QMessageBox.information(
                self, 'Build Complete',
                f'Routing database built successfully:\n\n{message}\n\n'
                'The database path has been automatically loaded into the '
                'main Offline Router window - now you can start routing.'
            )
        else:
            self._set_status('Build failed. See log for details.', 'red')
            QMessageBox.critical(self, 'Build Failed', message)

    def closeEvent(self, event):
        if self._worker and self._worker.isRunning():
            self._worker.terminate()
            self._worker.wait(2000)
        super().closeEvent(event)
