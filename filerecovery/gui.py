"""
Finder-style GUI for the file recovery tool.

Run with:
    python -m filerecovery.gui
or (after pip install -e .):
    filerecovery-gui
"""

import os
import platform
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from PyQt6.QtCore import (
    QAbstractTableModel,
    QByteArray,
    QModelIndex,
    QSortFilterProxyModel,
    Qt,
    QThread,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import QAction, QColor, QFont, QImage, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHeaderView,
    QLabel,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QSizePolicy,
    QSplitter,
    QStatusBar,
    QTableView,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

try:
    from .metadata import make_thumbnail as _make_thumbnail
    _THUMBS_AVAILABLE = True
except ImportError:
    _THUMBS_AVAILABLE = False
    def _make_thumbnail(*a, **kw): return None  # noqa: E731

from .db import FoundFile, ScanDB
from .disks import Disk, list_disks
from .recover import RecoveryError, recover_file
from .scanner import scan as run_scan
from .source import Source


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


_TYPE_ICON = {
    "JPEG": "🖼", "PNG": "🖼", "GIF": "🖼", "BMP": "🖼",
    "PDF":  "📄",
    "ZIP":  "📦",
    "WAV":  "🎵",
    "AVI":  "🎬", "MP4": "🎬", "MOV": "🎬",
    "SQLite": "🗄",
}

_IMAGE_TYPES = frozenset({"JPEG", "PNG", "GIF", "BMP"})
THUMB_SIZE = 48  # px — thumbnail cell size


def _fmt_date(ts: Optional[float]) -> str:
    if ts is None:
        return ""
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    except (OSError, ValueError, OverflowError):
        return ""


# ---------------------------------------------------------------------------
# Table model
# ---------------------------------------------------------------------------

# Column indices
_COL_THUMB  = 0
_COL_EXT    = 1
_COL_TYPE   = 2
_COL_DATE   = 3
_COL_SIZE   = 4
_COL_OFFSET = 5
_COL_STATUS = 6
_NUM_COLS   = 7


class FoundFilesModel(QAbstractTableModel):
    COLUMNS = ["", "Ext", "Type", "Date", "Size", "Offset", "Status"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._files: List[FoundFile] = []
        self._thumbs: dict = {}  # found_id → QPixmap

    def set_files(self, files: List[FoundFile]):
        self.beginResetModel()
        self._files = list(files)
        self._thumbs.clear()
        self.endResetModel()

    def set_thumbnail(self, found_id: int, pixmap: QPixmap):
        for row, f in enumerate(self._files):
            if f.id == found_id:
                self._thumbs[found_id] = pixmap
                idx = self.index(row, _COL_THUMB)
                self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DecorationRole])
                return

    def rowCount(self, parent=QModelIndex()):
        return len(self._files)

    def columnCount(self, parent=QModelIndex()):
        return _NUM_COLS

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.COLUMNS[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        f = self._files[index.row()]
        col = index.column()

        if role == Qt.ItemDataRole.DecorationRole and col == _COL_THUMB:
            return self._thumbs.get(f.id)

        if role == Qt.ItemDataRole.DisplayRole:
            if col == _COL_THUMB:
                return None
            if col == _COL_EXT:
                return f.extension.lstrip(".")
            if col == _COL_TYPE:
                return f.file_type
            if col == _COL_DATE:
                return _fmt_date(f.file_date)
            if col == _COL_SIZE:
                return _fmt_size(f.length)
            if col == _COL_OFFSET:
                return f"{f.offset:,}"
            if col == _COL_STATUS:
                if f.recovered_path:
                    return "Recovered"
                if f.truncated:
                    return "Truncated"
                return ""

        elif role == Qt.ItemDataRole.ForegroundRole:
            if col == _COL_STATUS:
                if f.recovered_path:
                    return QColor("#34C759")
                if f.truncated:
                    return QColor("#FF9500")

        elif role == Qt.ItemDataRole.TextAlignmentRole:
            if col in (_COL_SIZE, _COL_OFFSET):
                return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        elif role == Qt.ItemDataRole.UserRole:
            return f.id

        return None

    def get_file(self, row: int) -> Optional[FoundFile]:
        if 0 <= row < len(self._files):
            return self._files[row]
        return None

    def get_all_files(self) -> List[FoundFile]:
        return list(self._files)


# ---------------------------------------------------------------------------
# Background workers
# ---------------------------------------------------------------------------

class ScanWorker(QThread):
    progress = pyqtSignal(int, int)   # bytes_done, total
    finished = pyqtSignal(int)        # total found
    error    = pyqtSignal(str)

    def __init__(self, device: str, db_path: str, scan_id: int, start_offset: int = 0):
        super().__init__()
        self.device       = device
        self.db_path      = db_path
        self.scan_id      = scan_id
        self.start_offset = start_offset
        self._stop        = False

    def stop(self):
        self._stop = True

    def run(self):
        try:
            with Source(self.device) as source, ScanDB(self.db_path) as db:
                count = run_scan(
                    source, db, self.scan_id,
                    start_offset=self.start_offset,
                    progress_cb=lambda done, total: self.progress.emit(done, total),
                    stop_check=lambda: self._stop,
                )
            self.finished.emit(count)
        except PermissionError:
            self.error.emit(
                f"Permission denied opening {self.device}.\n\n"
                "Raw disk access requires sudo. Run the scan from the terminal:\n"
                f"  sudo filerecovery scan {self.device} --db {self.db_path}"
            )
        except Exception as e:
            self.error.emit(str(e))


class RecoverWorker(QThread):
    progress = pyqtSignal(int, int, str)  # done, total, filename
    finished = pyqtSignal(int)
    error    = pyqtSignal(str)

    def __init__(self, source_path: str, db_path: str, files: list[FoundFile], dest: str):
        super().__init__()
        self.source_path = source_path
        self.db_path     = db_path
        self.files       = files
        self.dest        = dest

    def run(self):
        try:
            with Source(self.source_path) as source, ScanDB(self.db_path) as db:
                total = len(self.files)
                for i, f in enumerate(self.files):
                    out = recover_file(source, db, f, self.dest)
                    self.progress.emit(i + 1, total, os.path.basename(out))
            self.finished.emit(total)
        except RecoveryError as e:
            self.error.emit(str(e))
        except Exception as e:
            self.error.emit(str(e))


# ---------------------------------------------------------------------------
# Thumbnail loader
# ---------------------------------------------------------------------------

class ThumbnailLoader(QThread):
    """Loads image thumbnails in a background thread; emits one signal per file."""
    thumbnail_ready = pyqtSignal(int, QPixmap)  # found_id, pixmap

    def __init__(self, source_path: str, files: List[FoundFile],
                 thumb_size: int = THUMB_SIZE):
        super().__init__()
        self.source_path = source_path
        self.files = files
        self.thumb_size = thumb_size
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        if not _THUMBS_AVAILABLE:
            return
        try:
            with Source(self.source_path) as source:
                for f in self.files:
                    if self._stop:
                        break
                    if f.file_type not in _IMAGE_TYPES:
                        continue
                    data = source.read_at(f.offset, min(f.length, 512 * 1024))
                    png_bytes = _make_thumbnail(f.file_type, data, self.thumb_size)
                    if not png_bytes:
                        continue
                    ba = QByteArray(png_bytes)
                    img = QImage.fromData(ba, "PNG")
                    if img.isNull():
                        continue
                    self.thumbnail_ready.emit(f.id, QPixmap.fromImage(img))
        except Exception:
            pass  # thumbnail failures must not surface to the user


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("File Recovery")
        self.resize(1100, 700)

        self.db_path: Optional[str]       = None
        self.db: Optional[ScanDB]         = None
        self.current_scan_id: Optional[int] = None
        self.scan_worker: Optional[ScanWorker]     = None
        self.recover_worker: Optional[RecoverWorker] = None
        self._thumb_loader: Optional[ThumbnailLoader] = None
        self.disks: List[Disk] = []
        self._poll_timer: Optional[QTimer] = None

        self._build_ui()
        self._apply_style()
        self._load_disks()

    # ------------------------------------------------------------------ build

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Toolbar
        tb = QToolBar()
        tb.setMovable(False)
        tb.setFloatable(False)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, tb)

        self.act_open_db = QAction("Open / New DB…", self)
        self.act_open_db.triggered.connect(self._open_db)
        tb.addAction(self.act_open_db)

        tb.addSeparator()

        self.act_scan = QAction("Scan Disk…", self)
        self.act_scan.setEnabled(False)
        self.act_scan.triggered.connect(self._start_scan)
        tb.addAction(self.act_scan)

        self.act_stop = QAction("Stop", self)
        self.act_stop.setEnabled(False)
        self.act_stop.triggered.connect(self._stop_scan)
        tb.addAction(self.act_stop)

        tb.addSeparator()

        self.act_recover = QAction("Recover Selected", self)
        self.act_recover.setEnabled(False)
        self.act_recover.triggered.connect(self._recover_selected)
        tb.addAction(self.act_recover)

        self.act_recover_all = QAction("Recover All", self)
        self.act_recover_all.setEnabled(False)
        self.act_recover_all.triggered.connect(self._recover_all)
        tb.addAction(self.act_recover_all)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)

        self.type_filter = QComboBox()
        self.type_filter.addItem("All Types")
        self.type_filter.setMinimumWidth(140)
        self.type_filter.currentTextChanged.connect(self._apply_filter)
        tb.addWidget(self.type_filter)

        # Splitter: sidebar | file table
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(1)
        root.addWidget(splitter)

        # Sidebar
        self.sidebar = QTreeWidget()
        self.sidebar.setHeaderHidden(True)
        self.sidebar.setMinimumWidth(180)
        self.sidebar.setMaximumWidth(260)
        self.sidebar.setRootIsDecorated(False)
        self.sidebar.setIndentation(18)
        self.sidebar.itemClicked.connect(self._sidebar_clicked)

        section_font = QFont()
        section_font.setPointSize(10)
        section_font.setBold(True)

        self._sec_devices = QTreeWidgetItem(self.sidebar, ["DEVICES"])
        self._sec_devices.setFlags(Qt.ItemFlag.ItemIsEnabled)
        self._sec_devices.setFont(0, section_font)
        self._sec_devices.setForeground(0, QColor("#8E8E93"))

        self._sec_scans = QTreeWidgetItem(self.sidebar, ["SCANS"])
        self._sec_scans.setFlags(Qt.ItemFlag.ItemIsEnabled)
        self._sec_scans.setFont(0, section_font)
        self._sec_scans.setForeground(0, QColor("#8E8E93"))

        splitter.addWidget(self.sidebar)

        # Main panel
        main_panel = QWidget()
        main_layout = QVBoxLayout(main_panel)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        self.model = FoundFilesModel()
        self.proxy = QSortFilterProxyModel()
        self.proxy.setSourceModel(self.model)
        self.proxy.setFilterKeyColumn(_COL_TYPE)

        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setSortingEnabled(True)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(THUMB_SIZE + 8)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hdr.setStretchLastSection(True)
        self.table.setColumnWidth(_COL_THUMB,  THUMB_SIZE + 4)
        self.table.setColumnWidth(_COL_EXT,    46)
        self.table.setColumnWidth(_COL_TYPE,   72)
        self.table.setColumnWidth(_COL_DATE,   90)
        self.table.setColumnWidth(_COL_SIZE,   80)
        self.table.setColumnWidth(_COL_OFFSET, 140)
        self.table.selectionModel().selectionChanged.connect(self._selection_changed)

        main_layout.addWidget(self.table)

        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setMaximumHeight(4)
        self.progress_bar.hide()
        main_layout.addWidget(self.progress_bar)

        splitter.addWidget(main_panel)

        # Preview panel (right side)
        preview_panel = QWidget()
        preview_panel.setMinimumWidth(200)
        preview_panel.setMaximumWidth(240)
        pv_layout = QVBoxLayout(preview_panel)
        pv_layout.setContentsMargins(12, 12, 12, 12)
        pv_layout.setSpacing(8)

        pv_title = QLabel("Preview")
        pv_title.setStyleSheet("font-weight: 600; font-size: 12px; color: #8E8E93;")
        pv_layout.addWidget(pv_title)

        self.preview_img = QLabel()
        self.preview_img.setFixedSize(192, 192)
        self.preview_img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_img.setStyleSheet(
            "border: 1px solid #D1D1D6; background: #FAFAFA; border-radius: 4px;"
        )
        pv_layout.addWidget(self.preview_img)

        self.preview_meta = QLabel()
        self.preview_meta.setWordWrap(True)
        self.preview_meta.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.preview_meta.setStyleSheet("font-size: 11px; color: #3A3A3C; line-height: 1.5;")
        pv_layout.addWidget(self.preview_meta)
        pv_layout.addStretch()

        splitter.addWidget(preview_panel)
        splitter.setSizes([210, 740, 210])

        # Status bar
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self._status_label = QLabel("No database open")
        self.status_bar.addWidget(self._status_label)
        self._progress_label = QLabel("")
        self.status_bar.addPermanentWidget(self._progress_label)

    def _apply_style(self):
        self.setStyleSheet("""
            QMainWindow { background: #FFFFFF; }

            QTreeWidget {
                background: #F2F2F7;
                border: none;
                border-right: 1px solid #D1D1D6;
                font-size: 13px;
                color: #1C1C1E;
                outline: none;
            }
            QTreeWidget::item {
                height: 32px;
                padding-left: 4px;
                border-radius: 6px;
                color: #1C1C1E;
            }
            QTreeWidget::item:selected {
                background: #007AFF;
                color: white;
            }
            QTreeWidget::item:hover:!selected {
                background: #E5E5EA;
            }

            QTableView {
                background: white;
                alternate-background-color: #F9F9FB;
                border: none;
                font-size: 13px;
                selection-background-color: #D0E8FF;
                selection-color: black;
                outline: none;
            }
            QHeaderView::section {
                background: #F2F2F7;
                border: none;
                border-bottom: 1px solid #D1D1D6;
                border-right: 1px solid #E5E5EA;
                padding: 4px 8px;
                font-size: 12px;
                font-weight: 600;
                color: #3A3A3C;
            }

            QToolBar {
                background: #F5F5F7;
                border-bottom: 1px solid #D1D1D6;
                spacing: 6px;
                padding: 4px 8px;
            }
            QToolButton {
                border: 1px solid #C7C7CC;
                border-radius: 6px;
                padding: 3px 10px;
                background: white;
                color: #1C1C1E;
                font-size: 13px;
            }
            QToolButton:hover   { background: #E5E5EA; color: #1C1C1E; }
            QToolButton:pressed { background: #D1D1D6; color: #1C1C1E; }
            QToolButton:disabled { color: #AEAEB2; border-color: #E5E5EA; background: #F5F5F7; }

            QStatusBar {
                background: #F2F2F7;
                border-top: 1px solid #D1D1D6;
                font-size: 12px;
                color: #3A3A3C;
            }
            QComboBox {
                border: 1px solid #C7C7CC;
                border-radius: 6px;
                padding: 3px 8px;
                background: white;
                color: #1C1C1E;
                font-size: 13px;
            }
            QComboBox QAbstractItemView {
                color: #1C1C1E;
                background: white;
                selection-background-color: #007AFF;
                selection-color: white;
            }
            QProgressBar {
                background: #E5E5EA;
                border: none;
            }
            QProgressBar::chunk { background: #007AFF; }

            /* Preview panel */
            QLabel#preview_img {
                border: 1px solid #D1D1D6;
                background: #FAFAFA;
                border-radius: 4px;
            }
        """)

    # ------------------------------------------------------------------ disks

    def _load_disks(self):
        try:
            self.disks = list_disks()
        except Exception:
            self.disks = []

        while self._sec_devices.childCount():
            self._sec_devices.removeChild(self._sec_devices.child(0))

        for disk in self.disks:
            tag   = "[ext]" if disk.removable else "[int]"
            label = f"{tag}  {disk.description}  ({disk.device})"
            item  = QTreeWidgetItem([label])
            item.setData(0, Qt.ItemDataRole.UserRole, disk)
            self._sec_devices.addChild(item)

        self._sec_devices.setExpanded(True)
        self._sec_scans.setExpanded(True)

    # ------------------------------------------------------------------ DB

    def _open_db(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Open or Create Scan Database",
            str(Path.home()),
            "SQLite Database (*.db);;All Files (*)",
            options=QFileDialog.Option.DontConfirmOverwrite,
        )
        if not path:
            return
        if self.db:
            self.db.close()
        self.db_path = path
        self.db = ScanDB(path)
        self.act_scan.setEnabled(True)
        self._status_label.setText(f"DB: {os.path.basename(path)}")
        self._refresh_scans()

    def _refresh_scans(self):
        if not self.db:
            return

        while self._sec_scans.childCount():
            self._sec_scans.removeChild(self._sec_scans.child(0))

        for s in self.db.list_scans():
            pct  = int(s["bytes_scanned"] / s["source_size"] * 100) if s["source_size"] else 0
            if s["finished_at"]:
                badge = "✓"
                badge_color = QColor("#34C759")
            else:
                badge = f"{pct}%"
                badge_color = QColor("#FF9500")
            label = f"{badge}  Scan {s['id']} — {os.path.basename(s['source_path'])}"
            item  = QTreeWidgetItem([label])
            item.setData(0, Qt.ItemDataRole.UserRole, s["id"])
            item.setForeground(0, QColor("#1C1C1E"))
            # Colour just the badge portion isn't possible in a single item,
            # but we can tint the whole row based on status
            if not s["finished_at"]:
                item.setForeground(0, QColor("#FF9500"))
            self._sec_scans.addChild(item)

        self._sec_scans.setExpanded(True)

    # ------------------------------------------------------------------ sidebar

    def _sidebar_clicked(self, item: QTreeWidgetItem, _col: int):
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(data, int):
            self._load_scan(data)

    # ------------------------------------------------------------------ file list

    def _load_scan(self, scan_id: int, start_thumbs: bool = False):
        if not self.db:
            return
        self.current_scan_id = scan_id
        files = list(self.db.list_found_files(scan_id))
        self.model.set_files(files)

        # Rebuild type filter
        types = sorted({f.file_type for f in files})
        self.type_filter.blockSignals(True)
        self.type_filter.clear()
        self.type_filter.addItem("All Types")
        for t in types:
            self.type_filter.addItem(t)
        self.type_filter.blockSignals(False)

        scan = self.db.get_scan(scan_id)
        src  = os.path.basename(scan["source_path"]) if scan else "?"
        self._status_label.setText(
            f"Scan {scan_id}  ·  {src}  ·  {len(files):,} files found"
        )
        self.act_recover_all.setEnabled(len(files) > 0)
        self._selection_changed()

        if start_thumbs and scan:
            self._start_thumb_loader(scan["source_path"], files)

    def _start_thumb_loader(self, source_path: str, files: List[FoundFile]):
        if self._thumb_loader and self._thumb_loader.isRunning():
            self._thumb_loader.stop()
            self._thumb_loader.wait(500)
        image_files = [f for f in files if f.file_type in _IMAGE_TYPES]
        if not image_files:
            return
        self._thumb_loader = ThumbnailLoader(source_path, image_files)
        self._thumb_loader.thumbnail_ready.connect(self.model.set_thumbnail)
        self._thumb_loader.start()

    def _apply_filter(self, text: str):
        if text == "All Types":
            self.proxy.setFilterFixedString("")
        else:
            self.proxy.setFilterFixedString(text)

    def _selection_changed(self):
        rows = self.table.selectionModel().selectedRows()
        n = len(rows)
        self.act_recover.setEnabled(n > 0)
        self._progress_label.setText(f"{n} selected" if n else "")
        self._update_preview(rows[0] if n == 1 else None)

    def _update_preview(self, proxy_index):
        if proxy_index is None:
            self.preview_img.clear()
            self.preview_meta.clear()
            return
        src_idx = self.proxy.mapToSource(proxy_index)
        f = self.model.get_file(src_idx.row())
        if f is None:
            return

        # Image
        pixmap = self.model._thumbs.get(f.id)
        if pixmap:
            scaled = pixmap.scaled(
                192, 192,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.preview_img.setPixmap(scaled)
        else:
            icon = _TYPE_ICON.get(f.file_type, "📁")
            self.preview_img.setText(f'<span style="font-size:48px">{icon}</span>')
            self.preview_img.setTextFormat(Qt.TextFormat.RichText)

        # Metadata
        name = f"{f.file_type.lower()}_{f.offset:012d}.{f.extension}"
        date_str = _fmt_date(f.file_date) or "—"
        status = "Recovered" if f.recovered_path else ("Truncated" if f.truncated else "Found")
        meta = (
            f"<b>{name}</b><br>"
            f"<br>Type: {f.file_type}"
            f"<br>Size: {_fmt_size(f.length)}"
            f"<br>Date: {date_str}"
            f"<br>Offset: {f.offset:,}"
            f"<br>Status: {status}"
        )
        if f.recovered_path:
            meta += f"<br><br>Saved&nbsp;to:<br>{f.recovered_path}"
        self.preview_meta.setText(meta)

    # ------------------------------------------------------------------ scan

    def _pick_disk(self) -> Optional[Disk]:
        """Show a simple list dialog to choose which disk to scan."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Select Disk to Scan")
        dlg.setMinimumWidth(480)
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel("Choose a disk:"))

        lst = QListWidget()
        for d in self.disks:
            kind = "Removable" if d.removable else "Fixed"
            lst.addItem(f"{d.device}  —  {d.description}  ({kind}, {_fmt_size(d.size_bytes)})")
        lst.setCurrentRow(0)
        lst.itemDoubleClicked.connect(dlg.accept)
        layout.addWidget(lst)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        layout.addWidget(btns)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        row = lst.currentRow()
        if row < 0:
            return None
        return self.disks[row]

    def _start_scan(self):
        if not self.db_path:
            QMessageBox.warning(self, "No database", "Open a scan database first.")
            return

        # Prefer sidebar selection, otherwise prompt
        disk: Optional[Disk] = None
        for item in self.sidebar.selectedItems():
            d = item.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(d, Disk):
                disk = d
                break

        if not disk:
            if not self.disks:
                QMessageBox.warning(self, "No disks", "No disks found.")
                return
            disk = self._pick_disk()
            if disk is None:
                return

        reply = QMessageBox.information(
            self, "Sudo Required",
            f"Scanning {disk.device} requires root access.\n\n"
            "If the scan fails with a permission error, use the CLI instead:\n"
            f"    sudo filerecovery scan {disk.device} --db {self.db_path}",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
        )
        if reply == QMessageBox.StandardButton.Cancel:
            return

        with ScanDB(self.db_path) as db:
            scan_id = db.start_scan(disk.device, disk.size_bytes)

        # Re-open db so GUI has a live handle
        if self.db:
            self.db.close()
        self.db = ScanDB(self.db_path)

        self.scan_worker = ScanWorker(disk.device, self.db_path, scan_id)
        self.scan_worker.progress.connect(self._on_scan_progress)
        self.scan_worker.finished.connect(self._on_scan_finished)
        self.scan_worker.error.connect(self._on_scan_error)

        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.show()
        self.act_scan.setEnabled(False)
        self.act_stop.setEnabled(True)
        self.scan_worker.start()

        self._refresh_scans()
        self._load_scan(scan_id)

        # Poll every 3 s to show new file hits
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(lambda: self._load_scan(scan_id))
        self._poll_timer.start(3000)

    def _stop_scan(self):
        if self.scan_worker:
            self.scan_worker.stop()

    def _on_scan_progress(self, done: int, total: int):
        pct = int(done / total * 100) if total else 0
        self.progress_bar.setValue(pct)
        self._progress_label.setText(f"{_fmt_size(done)} / {_fmt_size(total)}  ({pct}%)")

    def _on_scan_finished(self, count: int):
        self._teardown_scan(f"Scan complete — {count:,} files found")

    def _on_scan_error(self, msg: str):
        self._teardown_scan("")
        QMessageBox.critical(self, "Scan Error", msg)

    def _teardown_scan(self, status: str):
        self.progress_bar.hide()
        self.act_scan.setEnabled(True)
        self.act_stop.setEnabled(False)
        if status:
            self._progress_label.setText(status)
        if self._poll_timer:
            self._poll_timer.stop()
        self._refresh_scans()
        if self.current_scan_id is not None:
            self._load_scan(self.current_scan_id, start_thumbs=True)

    # ------------------------------------------------------------------ recover

    def _get_selected_files(self) -> list[FoundFile]:
        files = []
        for idx in self.table.selectionModel().selectedRows():
            src = self.proxy.mapToSource(idx)
            f = self.model.get_file(src.row())
            if f:
                files.append(f)
        return files

    def _recover_selected(self):
        self._do_recover(self._get_selected_files())

    def _recover_all(self):
        self._do_recover(self.model.get_all_files())

    def _do_recover(self, files: list[FoundFile]):
        if not files or not self.db or self.current_scan_id is None:
            return
        scan = self.db.get_scan(self.current_scan_id)
        if scan is None:
            return

        dest = QFileDialog.getExistingDirectory(
            self, "Choose Recovery Destination", str(Path.home())
        )
        if not dest:
            return

        self.recover_worker = RecoverWorker(scan["source_path"], self.db_path, files, dest)
        self.recover_worker.progress.connect(self._on_recover_progress)
        self.recover_worker.finished.connect(self._on_recover_finished)
        self.recover_worker.error.connect(self._on_recover_error)

        self.progress_bar.setRange(0, len(files))
        self.progress_bar.setValue(0)
        self.progress_bar.show()
        self.act_recover.setEnabled(False)
        self.act_recover_all.setEnabled(False)
        self.recover_worker.start()

    def _on_recover_progress(self, done: int, total: int, name: str):
        self.progress_bar.setValue(done)
        self._progress_label.setText(f"Recovering {done}/{total}:  {name}")

    def _on_recover_finished(self, count: int):
        self.progress_bar.hide()
        self.act_recover.setEnabled(True)
        self.act_recover_all.setEnabled(True)
        self._progress_label.setText(f"Recovered {count:,} files")
        if self.current_scan_id is not None:
            self._load_scan(self.current_scan_id)
        QMessageBox.information(self, "Done", f"Recovered {count:,} files.")

    def _on_recover_error(self, msg: str):
        self.progress_bar.hide()
        self.act_recover.setEnabled(True)
        self.act_recover_all.setEnabled(True)
        QMessageBox.critical(self, "Recovery Error", msg)

    # ------------------------------------------------------------------ close

    def closeEvent(self, event):
        if self._thumb_loader and self._thumb_loader.isRunning():
            self._thumb_loader.stop()
            self._thumb_loader.wait(2000)
        if self.scan_worker and self.scan_worker.isRunning():
            self.scan_worker.stop()
            self.scan_worker.wait(3000)
        if self.db:
            self.db.close()
        event.accept()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("File Recovery")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
