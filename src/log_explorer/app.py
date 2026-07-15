#!/usr/bin/env python3
"""
Log Explorer

A PyQt6 GUI for browsing, filtering, and inspecting pylogfile (.plflog / legacy
.hdf / .json) log files. Covers at least the functionality of pylogfile's
`lumber` CLI: opening one or more log files at once, filtering by log level,
search terms, and timestamp range, and inspecting per-file log level
definitions and general file metadata.
"""

from __future__ import annotations

import sys
import os
import datetime
from dataclasses import dataclass

import h5py
from colorama import Fore

from pylogfile.base import (
	LogPile, LogEntry, LogFormat,
	markdown, level_to_str, find_level_in_list,
)

from PyQt6.QtCore import (
	Qt, QAbstractTableModel, QModelIndex, QSortFilterProxyModel, QDateTime, pyqtSignal
)
from PyQt6.QtGui import QAction, QColor, QBrush
from PyQt6.QtWidgets import (
	QApplication, QMainWindow, QDockWidget, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
	QTableView, QLineEdit, QComboBox, QCheckBox, QDateTimeEdit, QPushButton,
	QTabWidget, QTreeWidget, QTreeWidgetItem, QTableWidget, QTableWidgetItem,
	QAbstractItemView, QFileDialog, QMessageBox, QLabel,
)


# ============================================================================
# Small standalone helpers
# ============================================================================

def _safe_epoch(dt: datetime.datetime) -> float:
	""" Converts a (possibly naive OR timezone-aware) datetime to a plain epoch
	float. pylogfile timestamps are naive when produced live or loaded from
	JSON/legacy HDF, but timezone-aware (UTC) when loaded from a v1 compressed
	.plflog file. Comparing naive/aware datetimes directly raises TypeError, so
	everywhere we need to compare or sort timestamps we go through this
	epoch-float representation instead. """
	try:
		return dt.timestamp()
	except Exception:
		return 0.0


def format_timestamp(dt: datetime.datetime) -> str:
	try:
		return dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
	except Exception:
		return str(dt)


def _to_qdatetime(dt: datetime.datetime) -> QDateTime:
	return QDateTime.fromMSecsSinceEpoch(int(_safe_epoch(dt) * 1000))


def humanize_bytes(n: int) -> str:
	size = float(n)
	for unit in ("B", "KB", "MB"):
		if size < 1024:
			return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
		size /= 1024
	return f"{size:.1f} GB"


_PLAIN_FORMAT = LogFormat(use_color=False)


def clean_text(text: str) -> str:
	""" Strips pylogfile markdown escape sequences and collapses newlines/tabs
	so log messages/details render sanely as a single table cell. The full,
	un-cleaned text is still shown via tooltip. """
	if not text:
		return ""
	try:
		cleaned = markdown(text, _PLAIN_FORMAT)
	except Exception:
		cleaned = text
	return cleaned.replace("\n", " ⏎ ").replace("\t", " ")


# ---- colorama code <-> QColor -----------------------------------------

_COLOR_NAME_BY_CODE = {}
for _attr in dir(Fore):
	if _attr.isupper():
		_COLOR_NAME_BY_CODE[getattr(Fore, _attr)] = _attr

_COLOR_HEX = {
	"BLACK": "#2b2b2b",
	"RED": "#c0392b",
	"GREEN": "#27ae60",
	"YELLOW": "#b7950b",
	"BLUE": "#2980b9",
	"MAGENTA": "#8e44ad",
	"CYAN": "#16a085",
	"WHITE": "#bdc3c7",
	"LIGHTBLACK_EX": "#7f8c8d",
	"LIGHTRED_EX": "#e74c3c",
	"LIGHTGREEN_EX": "#2ecc71",
	"LIGHTYELLOW_EX": "#f4d03f",
	"LIGHTBLUE_EX": "#3498db",
	"LIGHTMAGENTA_EX": "#bb8fce",
	"LIGHTCYAN_EX": "#48c9b0",
	"LIGHTWHITE_EX": "#ecf0f1",
}


def colorama_code_to_qcolor(code: str):
	if not code:
		return None
	name = _COLOR_NAME_BY_CODE.get(code)
	if not name:
		return None
	hexval = _COLOR_HEX.get(name)
	return QColor(hexval) if hexval else None


def describe_color(code: str) -> str:
	if not code:
		return "(default)"
	name = _COLOR_NAME_BY_CODE.get(code)
	return name if name else "(custom)"


def level_background_color(level: int, level_list: list):
	idx = find_level_in_list(level, level_list)
	if idx is None:
		return None
	ld = level_list[idx]
	color = colorama_code_to_qcolor(ld.label_color) or colorama_code_to_qcolor(ld.main_color)
	if color is None:
		return None
	faded = QColor(color)
	faded.setAlpha(70)
	return faded


# ============================================================================
# File loading
# ============================================================================

@dataclass
class LoadedFile:
	label: str
	path: str
	pile: LogPile
	raw_meta: dict
	file_size: int
	mtime: datetime.datetime


def peek_raw_metadata(path: str) -> dict:
	""" Reads file-level metadata directly from disk, independent of LogPile,
	since LogPile.load_plflog() doesn't retain format/encoding info on the
	object once loaded. Used purely for display in the Metadata panel. """
	ext = os.path.splitext(path)[1].lower()
	if ext == ".json":
		return {"container_format": "json"}
	try:
		with h5py.File(path, "r") as fh:
			if "_file_info_" in fh:
				mfi = fh["_file_info_"]
				meta = {}
				for k, v in mfi.attrs.items():
					if isinstance(v, (bytes, bytearray)):
						v = v.decode("utf-8", errors="replace")
					elif hasattr(v, "item"):
						try:
							v = v.item()
						except Exception:
							v = str(v)
					meta[k] = v
				meta.setdefault("container_format", "hdf5")
				return meta
			if "logs" in fh:
				g = fh["logs"]
				if {"message", "detail", "timestamp", "level"}.issubset(set(g.keys())):
					return {
						"container_format": "hdf5",
						"file_standard": "pylogfile.logpile",
						"format_version": "0.0",
						"encoding": "legacy",
					}
		return {"container_format": "hdf5"}
	except Exception:
		return {"container_format": "unknown"}


def describe_format(raw_meta: dict) -> str:
	if raw_meta.get("container_format") == "json":
		return "JSON"
	std = raw_meta.get("file_standard")
	if std == "pylogfile.logpile":
		ver = raw_meta.get("format_version")
		enc = raw_meta.get("encoding")
		bits = [f"v{ver}" if ver else None, enc]
		return "plflog (" + ", ".join(b for b in bits if b) + ")"
	return "HDF5 (unrecognized layout)"


def load_log_file(path: str) -> LoadedFile:
	if not os.path.isfile(path):
		raise FileNotFoundError(f"No such file: {path}")

	pile = LogPile()
	pile.terminal_output_enable = False

	ext = os.path.splitext(path)[1].lower()
	if ext == ".json":
		ok = pile.load_json(path)
	else:
		ok = pile.load_plflog(path)

	if not ok:
		raise ValueError("pylogfile reported a failure while reading this file.")

	stat = os.stat(path)
	return LoadedFile(
		label=os.path.basename(path),
		path=os.path.abspath(path),
		pile=pile,
		raw_meta=peek_raw_metadata(path),
		file_size=stat.st_size,
		mtime=datetime.datetime.fromtimestamp(stat.st_mtime),
	)


# ============================================================================
# Table model
# ============================================================================

@dataclass
class RowRef:
	local_index: int
	entry: LogEntry
	level_list: list


COLUMNS = ["#", "Idx", "Level", "Timestamp", "Message", "Detail"]
COL_SEQ, COL_IDX, COL_LEVEL, COL_TIME, COL_MSG, COL_DETAIL = range(len(COLUMNS))


class LogTableModel(QAbstractTableModel):
	""" Holds the (chronologically sorted) logs of a single opened file. Each
	opened file gets its own LogTableModel/view, shown in its own tab. """

	def __init__(self, parent=None):
		super().__init__(parent)
		self._rows: list[RowRef] = []

	def set_file(self, loaded_file: LoadedFile):
		self.beginResetModel()
		rows = [
			RowRef(local_index=i, entry=entry, level_list=loaded_file.pile.log_levels)
			for i, entry in enumerate(loaded_file.pile.logs)
		]
		rows.sort(key=lambda r: _safe_epoch(r.entry.timestamp))
		self._rows = rows
		self.endResetModel()

	def row_ref(self, row: int) -> RowRef:
		return self._rows[row]

	def rowCount(self, parent=QModelIndex()):
		return 0 if parent.isValid() else len(self._rows)

	def columnCount(self, parent=QModelIndex()):
		return 0 if parent.isValid() else len(COLUMNS)

	def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
		if role != Qt.ItemDataRole.DisplayRole:
			return None
		if orientation == Qt.Orientation.Horizontal:
			return COLUMNS[section]
		return section + 1

	def data(self, index, role=Qt.ItemDataRole.DisplayRole):
		if not index.isValid():
			return None
		ref = self._rows[index.row()]
		col = index.column()
		entry = ref.entry

		if role == Qt.ItemDataRole.DisplayRole:
			if col == COL_SEQ:
				return index.row() + 1
			if col == COL_IDX:
				return ref.local_index
			if col == COL_LEVEL:
				return level_to_str(entry.level, ref.level_list) or str(entry.level)
			if col == COL_TIME:
				return format_timestamp(entry.timestamp)
			if col == COL_MSG:
				return clean_text(entry.message)
			if col == COL_DETAIL:
				return clean_text(entry.detail)

		elif role == Qt.ItemDataRole.UserRole:
			# Raw, typed values used for sorting (see MainWindow: proxy.setSortRole).
			if col == COL_SEQ:
				return index.row()
			if col == COL_IDX:
				return ref.local_index
			if col == COL_LEVEL:
				return entry.level
			if col == COL_TIME:
				return _safe_epoch(entry.timestamp)
			if col == COL_MSG:
				return entry.message
			if col == COL_DETAIL:
				return entry.detail

		elif role == Qt.ItemDataRole.ToolTipRole:
			if col == COL_MSG:
				return entry.message
			if col == COL_DETAIL:
				return entry.detail
			if col == COL_TIME:
				return str(entry.timestamp)

		elif role == Qt.ItemDataRole.BackgroundRole:
			color = level_background_color(entry.level, ref.level_list)
			if color is not None:
				return QBrush(color)

		return None


# ============================================================================
# Filtering
# ============================================================================

class LogFilterProxyModel(QSortFilterProxyModel):
	""" Filters the flattened log table by level range, search terms, and
	timestamp range. This is the GUI equivalent of Lumberjack's `SHOW` command
	flags (--min/--max/--contains/--andcontains/--after/--before), applied
	live rather than one command at a time. Unlike pylogfile's
	SortConditions.matches_sort(), the time bounds here work independently
	(only one side needs to be set) and are compared as epoch floats so mixed
	naive/timezone-aware timestamps (see _safe_epoch) never crash a sort or
	filter pass. """

	def __init__(self, parent=None):
		super().__init__(parent)
		self.min_level = None
		self.max_level = None
		self.contains_any: list[str] = []
		self.contains_all: list[str] = []
		self.case_sensitive = False
		self.time_start = None
		self.time_end = None
		self.setDynamicSortFilter(True)

	def set_filters(self, min_level=None, max_level=None, contains_any=None, contains_all=None,
					 case_sensitive=False, time_start=None, time_end=None):
		self.min_level = min_level
		self.max_level = max_level
		self.contains_any = contains_any or []
		self.contains_all = contains_all or []
		self.case_sensitive = case_sensitive
		self.time_start = time_start
		self.time_end = time_end
		self.invalidateFilter()

	def filterAcceptsRow(self, source_row, source_parent):
		model = self.sourceModel()
		if model is None or source_row >= model.rowCount():
			return True
		ref = model.row_ref(source_row)
		entry = ref.entry

		if self.min_level is not None and entry.level < self.min_level:
			return False
		if self.max_level is not None and entry.level > self.max_level:
			return False

		if self.time_start is not None or self.time_end is not None:
			ts = _safe_epoch(entry.timestamp)
			if self.time_start is not None and ts < self.time_start:
				return False
			if self.time_end is not None and ts > self.time_end:
				return False

		if self.contains_any or self.contains_all:
			haystack = f"{entry.message}\n{entry.detail}"
			if not self.case_sensitive:
				haystack = haystack.lower()

			if self.contains_any:
				terms = self.contains_any if self.case_sensitive else [t.lower() for t in self.contains_any]
				if not any(t in haystack for t in terms):
					return False

			if self.contains_all:
				terms = self.contains_all if self.case_sensitive else [t.lower() for t in self.contains_all]
				if not all(t in haystack for t in terms):
					return False

		return True


# ============================================================================
# Filter panel (toggleable dock contents)
# ============================================================================

class FilterPanel(QWidget):
	filters_changed = pyqtSignal()

	def __init__(self, parent=None):
		super().__init__(parent)

		self._min_bound: datetime.datetime | None = None
		self._max_bound: datetime.datetime | None = None

		self.min_level_combo = QComboBox()
		self.max_level_combo = QComboBox()
		self.set_level_choices([])

		self.contains_any_edit = QLineEdit()
		self.contains_any_edit.setPlaceholderText("term1, term2, ...  (match ANY)")
		self.contains_all_edit = QLineEdit()
		self.contains_all_edit.setPlaceholderText("term1, term2, ...  (match ALL)")
		self.case_sensitive_check = QCheckBox("Case sensitive search")

		self.from_enable_check = QCheckBox("From")
		self.from_edit = QDateTimeEdit()
		self.from_edit.setCalendarPopup(True)
		self.from_edit.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
		self.from_edit.setDateTime(QDateTime.currentDateTime())
		self.from_edit.setEnabled(False)

		self.to_enable_check = QCheckBox("To")
		self.to_edit = QDateTimeEdit()
		self.to_edit.setCalendarPopup(True)
		self.to_edit.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
		self.to_edit.setDateTime(QDateTime.currentDateTime())
		self.to_edit.setEnabled(False)

		self.reset_range_button = QPushButton("Reset Range")
		self.clear_button = QPushButton("Clear Filters")

		form = QFormLayout()
		form.addRow("Min level:", self.min_level_combo)
		form.addRow("Max level:", self.max_level_combo)
		form.addRow(self.contains_any_edit)
		form.addRow(self.contains_all_edit)
		form.addRow(self.case_sensitive_check)

		time_row1 = QHBoxLayout()
		time_row1.addWidget(self.from_enable_check)
		time_row1.addWidget(self.from_edit)
		time_row2 = QHBoxLayout()
		time_row2.addWidget(self.to_enable_check)
		time_row2.addWidget(self.to_edit)

		buttons_row = QHBoxLayout()
		buttons_row.addWidget(self.reset_range_button)
		buttons_row.addWidget(self.clear_button)

		layout = QVBoxLayout(self)
		layout.addLayout(form)
		layout.addWidget(QLabel("Timestamp range:"))
		layout.addLayout(time_row1)
		layout.addLayout(time_row2)
		layout.addLayout(buttons_row)
		layout.addStretch(1)

		self.from_enable_check.toggled.connect(self.from_edit.setEnabled)
		self.to_enable_check.toggled.connect(self.to_edit.setEnabled)

		for w in (self.min_level_combo, self.max_level_combo):
			w.currentIndexChanged.connect(self.filters_changed)
		for w in (self.contains_any_edit, self.contains_all_edit):
			w.textChanged.connect(self.filters_changed)
		self.case_sensitive_check.toggled.connect(self.filters_changed)
		self.from_enable_check.toggled.connect(self.filters_changed)
		self.to_enable_check.toggled.connect(self.filters_changed)
		self.from_edit.dateTimeChanged.connect(self.filters_changed)
		self.to_edit.dateTimeChanged.connect(self.filters_changed)

		self.clear_button.clicked.connect(self.clear_filters)
		self.reset_range_button.clicked.connect(self.reset_range)

	def set_level_choices(self, levels):
		""" levels: iterable of (level_int, level_name) sorted by level_int. """
		for combo, sentinel in ((self.min_level_combo, "(No minimum)"), (self.max_level_combo, "(No maximum)")):
			prev = combo.currentData() if combo.count() else None
			combo.blockSignals(True)
			combo.clear()
			combo.addItem(sentinel, None)
			for level_int, level_name in levels:
				combo.addItem(f"{level_name} ({level_int})", level_int)
			if prev is not None:
				idx = combo.findData(prev)
				combo.setCurrentIndex(idx if idx >= 0 else 0)
			else:
				combo.setCurrentIndex(0)
			combo.blockSignals(False)

	def set_time_bounds(self, min_dt: datetime.datetime | None, max_dt: datetime.datetime | None):
		""" Called whenever the set of opened files changes, so the From/To
		fields are always pre-populated with the earliest/latest timestamp
		actually present across the currently opened files. Also remembered
		so 'Reset Range' can restore these bounds later. """
		self._min_bound = min_dt
		self._max_bound = max_dt
		if min_dt is not None:
			self.from_edit.blockSignals(True)
			self.from_edit.setDateTime(_to_qdatetime(min_dt))
			self.from_edit.blockSignals(False)
		if max_dt is not None:
			self.to_edit.blockSignals(True)
			self.to_edit.setDateTime(_to_qdatetime(max_dt))
			self.to_edit.blockSignals(False)
		self.filters_changed.emit()

	def reset_range(self):
		""" Resets the From/To fields back to the min/max timestamps across the
		currently opened files (does not change whether the range is enabled). """
		if self._min_bound is not None:
			self.from_edit.setDateTime(_to_qdatetime(self._min_bound))
		if self._max_bound is not None:
			self.to_edit.setDateTime(_to_qdatetime(self._max_bound))

	def clear_filters(self):
		self.min_level_combo.setCurrentIndex(0)
		self.max_level_combo.setCurrentIndex(0)
		self.contains_any_edit.clear()
		self.contains_all_edit.clear()
		self.case_sensitive_check.setChecked(False)
		self.from_enable_check.setChecked(False)
		self.to_enable_check.setChecked(False)
		self.filters_changed.emit()

	def build_filter_params(self) -> dict:
		def split_terms(text):
			return [t.strip() for t in text.split(",") if t.strip()]

		time_start = None
		time_end = None
		if self.from_enable_check.isChecked():
			time_start = self.from_edit.dateTime().toPyDateTime().timestamp()
		if self.to_enable_check.isChecked():
			time_end = self.to_edit.dateTime().toPyDateTime().timestamp()

		return dict(
			min_level=self.min_level_combo.currentData(),
			max_level=self.max_level_combo.currentData(),
			contains_any=split_terms(self.contains_any_edit.text()),
			contains_all=split_terms(self.contains_all_edit.text()),
			case_sensitive=self.case_sensitive_check.isChecked(),
			time_start=time_start,
			time_end=time_end,
		)


# ============================================================================
# Metadata panel (toggleable dock contents)
# ============================================================================

class MetadataPanel(QWidget):
	close_file_requested = pyqtSignal(str)
	close_all_requested = pyqtSignal()

	def __init__(self, parent=None):
		super().__init__(parent)
		self.tabs = QTabWidget()
		self._files_by_label: dict[str, LoadedFile] = {}

		# ---- Files tab ----
		self.files_tree = QTreeWidget()
		self.files_tree.setColumnCount(6)
		self.files_tree.setHeaderLabels(["File", "Format", "Logs", "Size", "Modified", "Path"])
		self.files_tree.setRootIsDecorated(False)

		close_selected_btn = QPushButton("Close Selected")
		close_all_btn = QPushButton("Close All")
		close_selected_btn.clicked.connect(self._emit_close_selected)
		close_all_btn.clicked.connect(self.close_all_requested)

		files_btn_row = QHBoxLayout()
		files_btn_row.addWidget(close_selected_btn)
		files_btn_row.addWidget(close_all_btn)
		files_btn_row.addStretch(1)

		files_tab = QWidget()
		files_layout = QVBoxLayout(files_tab)
		files_layout.addWidget(self.files_tree)
		files_layout.addLayout(files_btn_row)

		# ---- Log levels tab ----
		self.level_file_combo = QComboBox()
		self.level_file_combo.currentIndexChanged.connect(self._refresh_level_table)

		self.levels_table = QTableWidget(0, 7)
		self.levels_table.setHorizontalHeaderLabels(["Int", "Name", "Main", "Bold", "Quiet", "Alt", "Label"])
		self.levels_table.horizontalHeader().setStretchLastSection(True)
		self.levels_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)

		levels_tab = QWidget()
		levels_layout = QVBoxLayout(levels_tab)
		levels_layout.addWidget(self.level_file_combo)
		levels_layout.addWidget(self.levels_table)

		# ---- General info tab ----
		self.info_tree = QTreeWidget()
		self.info_tree.setColumnCount(2)
		self.info_tree.setHeaderLabels(["Property", "Value"])

		info_tab = QWidget()
		info_layout = QVBoxLayout(info_tab)
		info_layout.addWidget(self.info_tree)

		self.tabs.addTab(files_tab, "Files")
		self.tabs.addTab(levels_tab, "Log Levels")
		self.tabs.addTab(info_tab, "General Info")

		layout = QVBoxLayout(self)
		layout.addWidget(self.tabs)

	def _emit_close_selected(self):
		for item in self.files_tree.selectedItems():
			label = item.data(0, Qt.ItemDataRole.UserRole)
			if label:
				self.close_file_requested.emit(label)

	def refresh(self, files: list[LoadedFile]):
		self._files_by_label = {f.label: f for f in files}

		# --- Files tab ---
		self.files_tree.clear()
		for f in files:
			item = QTreeWidgetItem([
				f.label,
				describe_format(f.raw_meta),
				str(len(f.pile.logs)),
				humanize_bytes(f.file_size),
				f.mtime.strftime("%Y-%m-%d %H:%M:%S"),
				f.path,
			])
			item.setData(0, Qt.ItemDataRole.UserRole, f.label)
			self.files_tree.addTopLevelItem(item)
		for i in range(self.files_tree.columnCount()):
			self.files_tree.resizeColumnToContents(i)

		# --- Log levels tab ---
		prev = self.level_file_combo.currentText()
		self.level_file_combo.blockSignals(True)
		self.level_file_combo.clear()
		self.level_file_combo.addItems([f.label for f in files])
		if prev:
			idx = self.level_file_combo.findText(prev)
			if idx >= 0:
				self.level_file_combo.setCurrentIndex(idx)
		self.level_file_combo.blockSignals(False)
		self._refresh_level_table()

		# --- General info tab ---
		self.info_tree.clear()
		if files:
			summary_top = QTreeWidgetItem(["All Files (combined)", ""])
			self.info_tree.addTopLevelItem(summary_top)
			self._add_aggregate_children(summary_top, files)
		for f in files:
			top = QTreeWidgetItem([f.label, ""])
			self.info_tree.addTopLevelItem(top)
			self._add_info_children(top, f)
		self.info_tree.expandAll()
		for i in range(self.info_tree.columnCount()):
			self.info_tree.resizeColumnToContents(i)

	def _refresh_level_table(self):
		label = self.level_file_combo.currentText()
		f = self._files_by_label.get(label)
		self.levels_table.setRowCount(0)
		if f is None:
			return
		for ld in f.pile.log_levels:
			row = self.levels_table.rowCount()
			self.levels_table.insertRow(row)
			self.levels_table.setItem(row, 0, QTableWidgetItem(str(ld.level_int)))
			self.levels_table.setItem(row, 1, QTableWidgetItem(ld.level_name))
			colors = (ld.main_color, ld.bold_color, ld.quiet_color, ld.alt_color, ld.label_color)
			for col, code in zip(range(2, 7), colors):
				item = QTableWidgetItem(describe_color(code))
				qc = colorama_code_to_qcolor(code)
				if qc is not None:
					item.setBackground(qc)
				self.levels_table.setItem(row, col, item)

	def _add_aggregate_children(self, top: QTreeWidgetItem, files: list[LoadedFile]):
		total_logs = sum(len(f.pile.logs) for f in files)
		top.addChild(QTreeWidgetItem(["Files open", str(len(files))]))
		top.addChild(QTreeWidgetItem(["Total log count", str(total_logs)]))

		all_timestamps = [l.timestamp for f in files for l in f.pile.logs]
		if all_timestamps:
			first_ts = min(all_timestamps, key=_safe_epoch)
			last_ts = max(all_timestamps, key=_safe_epoch)
			top.addChild(QTreeWidgetItem(["First timestamp", format_timestamp(first_ts)]))
			top.addChild(QTreeWidgetItem(["Last timestamp", format_timestamp(last_ts)]))

		counts: dict[str, int] = {}
		for f in files:
			for l in f.pile.logs:
				name = level_to_str(l.level, f.pile.log_levels) or str(l.level)
				counts[name] = counts.get(name, 0) + 1
		counts_node = QTreeWidgetItem(["Counts by level", ""])
		top.addChild(counts_node)
		for name, n in sorted(counts.items(), key=lambda kv: -kv[1]):
			counts_node.addChild(QTreeWidgetItem([name, str(n)]))

	def _add_info_children(self, top: QTreeWidgetItem, f: LoadedFile):
		logs = f.pile.logs
		top.addChild(QTreeWidgetItem(["Log count", str(len(logs))]))
		top.addChild(QTreeWidgetItem(["File size", humanize_bytes(f.file_size)]))
		top.addChild(QTreeWidgetItem(["Modified (on disk)", f.mtime.strftime("%Y-%m-%d %H:%M:%S")]))
		top.addChild(QTreeWidgetItem(["Path", f.path]))

		meta_node = QTreeWidgetItem(["File format", ""])
		top.addChild(meta_node)
		for k, v in f.raw_meta.items():
			meta_node.addChild(QTreeWidgetItem([str(k), str(v)]))

		if logs:
			first_ts = min((l.timestamp for l in logs), key=_safe_epoch)
			last_ts = max((l.timestamp for l in logs), key=_safe_epoch)
			span = _safe_epoch(last_ts) - _safe_epoch(first_ts)
			top.addChild(QTreeWidgetItem(["First timestamp", format_timestamp(first_ts)]))
			top.addChild(QTreeWidgetItem(["Last timestamp", format_timestamp(last_ts)]))
			top.addChild(QTreeWidgetItem(["Timespan (s)", f"{span:.3f}"]))

			counts: dict[str, int] = {}
			for l in logs:
				name = level_to_str(l.level, f.pile.log_levels) or str(l.level)
				counts[name] = counts.get(name, 0) + 1
			counts_node = QTreeWidgetItem(["Counts by level", ""])
			top.addChild(counts_node)
			for name, n in sorted(counts.items(), key=lambda kv: -kv[1]):
				counts_node.addChild(QTreeWidgetItem([name, str(n)]))


# ============================================================================
# Main window
# ============================================================================

@dataclass
class FileTabEntry:
	loaded_file: LoadedFile
	table_model: LogTableModel
	proxy_model: LogFilterProxyModel
	table_view: QTableView


class MainWindow(QMainWindow):
	def __init__(self):
		super().__init__()
		self.setWindowTitle("Log Explorer")
		self.resize(1280, 760)
		self.setAcceptDrops(True)

		self.file_tabs: dict[str, FileTabEntry] = {}

		self.tabs_widget = QTabWidget()
		self.tabs_widget.setTabsClosable(True)
		self.tabs_widget.setMovable(True)
		self.tabs_widget.tabCloseRequested.connect(self._on_tab_close_requested)
		self.tabs_widget.currentChanged.connect(self._update_status_bar)
		self.setCentralWidget(self.tabs_widget)

		self.filter_panel = FilterPanel()
		self.filter_dock = QDockWidget("Filters", self)
		self.filter_dock.setObjectName("FiltersDock")
		self.filter_dock.setWidget(self.filter_panel)
		self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.filter_dock)

		self.metadata_panel = MetadataPanel()
		self.metadata_dock = QDockWidget("Metadata", self)
		self.metadata_dock.setObjectName("MetadataDock")
		self.metadata_dock.setWidget(self.metadata_panel)
		self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.metadata_dock)
		self.splitDockWidget(self.filter_dock, self.metadata_dock, Qt.Orientation.Vertical)

		self.filter_panel.filters_changed.connect(self.apply_filters)
		self.metadata_panel.close_file_requested.connect(self.close_file)
		self.metadata_panel.close_all_requested.connect(self.close_all_files)

		self._build_menu()
		self._build_toolbar()

		self.status_label = QLabel()
		self.statusBar().addPermanentWidget(self.status_label)

		self.apply_filters()

	# ---- menu / toolbar ----

	def _build_menu(self):
		file_menu = self.menuBar().addMenu("&File")

		open_action = QAction("&Open Log File(s)...", self)
		open_action.setShortcut("Ctrl+O")
		open_action.triggered.connect(self.open_files_dialog)
		file_menu.addAction(open_action)

		close_action = QAction("&Close Selected File", self)
		close_action.triggered.connect(self.close_selected_file)
		file_menu.addAction(close_action)

		close_all_action = QAction("Close &All Files", self)
		close_all_action.triggered.connect(self.close_all_files)
		file_menu.addAction(close_all_action)

		file_menu.addSeparator()
		exit_action = QAction("E&xit", self)
		exit_action.triggered.connect(self.close)
		file_menu.addAction(exit_action)

		view_menu = self.menuBar().addMenu("&View")
		view_menu.addAction(self.filter_dock.toggleViewAction())
		view_menu.addAction(self.metadata_dock.toggleViewAction())

	def _build_toolbar(self):
		tb = self.addToolBar("Main")
		tb.setObjectName("MainToolbar")

		open_action = QAction("Open...", self)
		open_action.triggered.connect(self.open_files_dialog)
		tb.addAction(open_action)

		tb.addSeparator()
		tb.addAction(self.filter_dock.toggleViewAction())
		tb.addAction(self.metadata_dock.toggleViewAction())

	# ---- drag & drop ----

	def dragEnterEvent(self, event):
		if event.mimeData().hasUrls():
			event.acceptProposedAction()

	def dropEvent(self, event):
		paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
		self._open_paths(paths)
		event.acceptProposedAction()

	# ---- file management ----

	def open_files_dialog(self):
		paths, _ = QFileDialog.getOpenFileNames(
			self, "Open Log File(s)", "",
			"Pylogfile Logs (*.plflog *.hdf *.json);;All Files (*)"
		)
		self._open_paths(paths)

	def _open_paths(self, paths: list[str]):
		for path in paths:
			if os.path.isfile(path):
				self.open_file(path)

	def open_file(self, path: str):
		try:
			lf = load_log_file(path)
		except Exception as e:
			QMessageBox.critical(self, "Failed to Open File", f"Could not open '{path}':\n{e}")
			return
		lf.label = self._unique_label(lf.label)

		table_model = LogTableModel()
		table_model.set_file(lf)
		proxy_model = LogFilterProxyModel()
		proxy_model.setSourceModel(table_model)
		proxy_model.setSortRole(Qt.ItemDataRole.UserRole)
		table_view = self._make_table_view(proxy_model)

		self.file_tabs[lf.label] = FileTabEntry(
			loaded_file=lf, table_model=table_model, proxy_model=proxy_model, table_view=table_view
		)
		self.tabs_widget.addTab(table_view, lf.label)
		self.tabs_widget.setCurrentWidget(table_view)

		self._refresh_all()

	@staticmethod
	def _make_table_view(proxy_model: LogFilterProxyModel) -> QTableView:
		view = QTableView()
		view.setModel(proxy_model)
		view.setSortingEnabled(True)
		view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
		view.setAlternatingRowColors(True)
		view.horizontalHeader().setStretchLastSection(True)
		view.verticalHeader().setVisible(False)
		view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
		return view

	def _unique_label(self, base_label: str) -> str:
		if base_label not in self.file_tabs:
			return base_label
		i = 2
		while f"{base_label} ({i})" in self.file_tabs:
			i += 1
		return f"{base_label} ({i})"

	def _label_for_tab_index(self, index: int) -> str | None:
		if index < 0:
			return None
		widget = self.tabs_widget.widget(index)
		for label, entry in self.file_tabs.items():
			if entry.table_view is widget:
				return label
		return None

	def _on_tab_close_requested(self, index: int):
		label = self._label_for_tab_index(index)
		if label is not None:
			self.close_file(label)

	def close_selected_file(self):
		label = self._label_for_tab_index(self.tabs_widget.currentIndex())
		if label is None:
			QMessageBox.information(self, "No Files Open", "There are no open files to close.")
			return
		self.close_file(label)

	def close_file(self, label: str):
		entry = self.file_tabs.pop(label, None)
		if entry is None:
			return
		idx = self.tabs_widget.indexOf(entry.table_view)
		if idx >= 0:
			self.tabs_widget.removeTab(idx)
		self._refresh_all()

	def close_all_files(self):
		self.file_tabs.clear()
		self.tabs_widget.clear()
		self._refresh_all()

	def _loaded_files(self) -> list[LoadedFile]:
		return [entry.loaded_file for entry in self.file_tabs.values()]

	def _refresh_all(self):
		files = self._loaded_files()
		self.filter_panel.set_level_choices(self._collect_levels(files))
		self.filter_panel.set_time_bounds(*self._collect_time_bounds(files))
		self.metadata_panel.refresh(files)
		self.apply_filters()

	@staticmethod
	def _collect_levels(files: list[LoadedFile]):
		levels: dict[int, str] = {}
		for lf in files:
			for ld in lf.pile.log_levels:
				levels[ld.level_int] = ld.level_name
		return sorted(levels.items())

	@staticmethod
	def _collect_time_bounds(files: list[LoadedFile]):
		timestamps = [l.timestamp for f in files for l in f.pile.logs]
		if not timestamps:
			return None, None
		return min(timestamps, key=_safe_epoch), max(timestamps, key=_safe_epoch)

	# ---- filtering ----

	def apply_filters(self):
		params = self.filter_panel.build_filter_params()
		for entry in self.file_tabs.values():
			entry.proxy_model.set_filters(**params)
		self._update_status_bar()

	def _update_status_bar(self, *_):
		total = sum(e.table_model.rowCount() for e in self.file_tabs.values())
		visible = sum(e.proxy_model.rowCount() for e in self.file_tabs.values())
		n_files = len(self.file_tabs)
		self.status_label.setText(f"{visible} / {total} logs shown   |   {n_files} file(s) open")


# ============================================================================
# Entry point
# ============================================================================

def main():
	app = QApplication(sys.argv)
	app.setApplicationName("Log Explorer")
	app.setOrganizationName("Grant Giesbrecht")

	win = MainWindow()
	win.show()

	for arg in sys.argv[1:]:
		if os.path.isfile(arg):
			win.open_file(arg)

	sys.exit(app.exec())


if __name__ == "__main__":
	main()
