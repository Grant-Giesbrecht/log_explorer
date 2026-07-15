import os
import sys
import tempfile
import unittest
import datetime

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from PyQt6.QtWidgets import QApplication
from pylogfile.base import LogPile, LogLevelDefinition

from log_explorer.app import (
	LogTableModel, LogFilterProxyModel, load_log_file, peek_raw_metadata,
	clean_text, describe_format, MainWindow,
)

_app = QApplication.instance() or QApplication(sys.argv)


def make_sample_pile():
	custom_levels = [
		LogLevelDefinition(1, "TRACE", label_color="\x1b[35m"),
		LogLevelDefinition(10, "DEBUG"),
		LogLevelDefinition(20, "INFO"),
		LogLevelDefinition(30, "WARNING"),
		LogLevelDefinition(40, "ERROR"),
	]
	pile = LogPile(level_list=custom_levels)
	pile.terminal_output_enable = False
	pile.add_log(20, "Starting >bold< run", detail="setup detail")
	pile.add_log(40, "Something failed", detail="stack trace goes here")
	pile.add_log(10, "debug spam", detail="")
	pile.add_log(30, "careful now", detail="warning detail")
	return pile


class TestLoadAndModel(unittest.TestCase):
	def setUp(self):
		self.tmpdir = tempfile.TemporaryDirectory()
		self.addCleanup(self.tmpdir.cleanup)
		self.path = os.path.join(self.tmpdir.name, "sample.plflog")
		make_sample_pile().save_plflog(self.path, file_version="1.0")

	def test_load_log_file_roundtrip(self):
		lf = load_log_file(self.path)
		self.assertEqual(len(lf.pile.logs), 4)
		self.assertEqual(lf.label, "sample.plflog")
		self.assertGreater(lf.file_size, 0)

	def test_raw_metadata_v1(self):
		meta = peek_raw_metadata(self.path)
		self.assertEqual(meta.get("file_standard"), "pylogfile.logpile")
		self.assertEqual(meta.get("encoding"), "compressed")

	def test_describe_format(self):
		meta = peek_raw_metadata(self.path)
		desc = describe_format(meta)
		self.assertIn("plflog", desc)

	def test_v0_legacy_metadata(self):
		v0_path = os.path.join(self.tmpdir.name, "legacy.plflog")
		make_sample_pile().save_plflog(v0_path, file_version="0.0")
		lf = load_log_file(v0_path)
		self.assertEqual(len(lf.pile.logs), 4)
		meta = peek_raw_metadata(v0_path)
		self.assertEqual(meta.get("encoding"), "legacy")

	def test_clean_text_strips_markdown(self):
		self.assertEqual(clean_text("Starting >bold< run"), "Starting bold run")
		self.assertEqual(clean_text("line1\nline2"), "line1 ⏎ line2")

	def test_table_model_holds_single_file_and_sorts(self):
		lf = load_log_file(self.path)
		model = LogTableModel()
		model.set_file(lf)
		self.assertEqual(model.rowCount(), 4)
		# Rows should be chronological.
		timestamps = [model.row_ref(i).entry.timestamp.timestamp() for i in range(model.rowCount())]
		self.assertEqual(timestamps, sorted(timestamps))


class TestFilterProxy(unittest.TestCase):
	def setUp(self):
		self.tmpdir = tempfile.TemporaryDirectory()
		self.addCleanup(self.tmpdir.cleanup)
		self.path = os.path.join(self.tmpdir.name, "sample.plflog")
		make_sample_pile().save_plflog(self.path, file_version="1.0")
		self.lf = load_log_file(self.path)
		self.model = LogTableModel()
		self.model.set_file(self.lf)
		self.proxy = LogFilterProxyModel()
		self.proxy.setSourceModel(self.model)

	def test_min_level_filter(self):
		self.proxy.set_filters(min_level=30)
		self.assertEqual(self.proxy.rowCount(), 2)  # WARNING, ERROR

	def test_max_level_filter(self):
		self.proxy.set_filters(max_level=10)
		self.assertEqual(self.proxy.rowCount(), 1)  # DEBUG

	def test_contains_any(self):
		self.proxy.set_filters(contains_any=["failed", "spam"])
		self.assertEqual(self.proxy.rowCount(), 2)

	def test_contains_all_requires_both(self):
		self.proxy.set_filters(contains_all=["Starting", "setup"])
		self.assertEqual(self.proxy.rowCount(), 1)
		self.proxy.set_filters(contains_all=["Starting", "nonexistent"])
		self.assertEqual(self.proxy.rowCount(), 0)

	def test_case_insensitive_by_default(self):
		self.proxy.set_filters(contains_any=["FAILED"])
		self.assertEqual(self.proxy.rowCount(), 1)

	def test_case_sensitive_option(self):
		self.proxy.set_filters(contains_any=["FAILED"], case_sensitive=True)
		self.assertEqual(self.proxy.rowCount(), 0)

	def test_time_range_one_sided(self):
		future = (datetime.datetime.now() + datetime.timedelta(days=1)).timestamp()
		self.proxy.set_filters(time_start=future)
		self.assertEqual(self.proxy.rowCount(), 0)

		past = (datetime.datetime.now() - datetime.timedelta(days=1)).timestamp()
		self.proxy.set_filters(time_start=past)
		self.assertEqual(self.proxy.rowCount(), 4)

	def test_no_filters_shows_everything(self):
		self.proxy.set_filters()
		self.assertEqual(self.proxy.rowCount(), 4)


class TestFilterPanelTimeBounds(unittest.TestCase):
	""" Covers auto-populating From/To with the min/max timestamp, and the
	Reset Range button restoring them after the user changes the fields. """

	def setUp(self):
		self.tmpdir = tempfile.TemporaryDirectory()
		self.addCleanup(self.tmpdir.cleanup)
		self.path = os.path.join(self.tmpdir.name, "sample.plflog")
		make_sample_pile().save_plflog(self.path, file_version="1.0")

	def test_auto_populate_and_reset_range(self):
		win = MainWindow()
		try:
			win.open_file(self.path)
			lf = list(win._loaded_files())[0]
			first_ts = min(l.timestamp for l in lf.pile.logs)
			last_ts = max(l.timestamp for l in lf.pile.logs)

			fp = win.filter_panel
			# Auto-populated to the file's actual min/max timestamp.
			self.assertAlmostEqual(
				fp.from_edit.dateTime().toPyDateTime().timestamp(), first_ts.timestamp(), delta=1
			)
			self.assertAlmostEqual(
				fp.to_edit.dateTime().toPyDateTime().timestamp(), last_ts.timestamp(), delta=1
			)

			# User nudges the range...
			fp.from_edit.setDateTime(fp.from_edit.dateTime().addDays(5))
			self.assertNotAlmostEqual(
				fp.from_edit.dateTime().toPyDateTime().timestamp(), first_ts.timestamp(), delta=1
			)

			# ...Reset Range puts it back.
			fp.reset_range()
			self.assertAlmostEqual(
				fp.from_edit.dateTime().toPyDateTime().timestamp(), first_ts.timestamp(), delta=1
			)
			self.assertAlmostEqual(
				fp.to_edit.dateTime().toPyDateTime().timestamp(), last_ts.timestamp(), delta=1
			)
		finally:
			win.close()


class TestMainWindowSmoke(unittest.TestCase):
	""" End-to-end smoke test: build the real MainWindow (offscreen), open
	files into their own tabs, exercise filters, drag-and-drop, and file
	close, and toggle both docks. """

	def setUp(self):
		self.tmpdir = tempfile.TemporaryDirectory()
		self.addCleanup(self.tmpdir.cleanup)
		self.path = os.path.join(self.tmpdir.name, "sample.plflog")
		make_sample_pile().save_plflog(self.path, file_version="1.0")

	def test_open_filter_close(self):
		win = MainWindow()
		try:
			win.open_file(self.path)
			self.assertIn("sample.plflog", win.file_tabs)
			entry = win.file_tabs["sample.plflog"]
			self.assertEqual(entry.table_model.rowCount(), 4)
			self.assertEqual(win.tabs_widget.count(), 1)

			win.filter_panel.min_level_combo.setCurrentIndex(
				win.filter_panel.min_level_combo.findData(30)
			)
			win.apply_filters()
			self.assertEqual(entry.proxy_model.rowCount(), 2)

			# Both docks should be toggleable without raising.
			win.filter_dock.setVisible(False)
			win.metadata_dock.setVisible(False)
			win.filter_dock.setVisible(True)
			win.metadata_dock.setVisible(True)

			win.close_file("sample.plflog")
			self.assertEqual(win.tabs_widget.count(), 0)
			self.assertNotIn("sample.plflog", win.file_tabs)
		finally:
			win.close()

	def test_each_file_gets_its_own_tab(self):
		win = MainWindow()
		try:
			win.open_file(self.path)
			win.open_file(self.path)
			self.assertEqual(len(win.file_tabs), 2)
			self.assertEqual(win.tabs_widget.count(), 2)
			self.assertIn("sample.plflog", win.file_tabs)
			self.assertIn("sample.plflog (2)", win.file_tabs)
			# Each tab's model holds only that file's own 4 rows, not 8 merged.
			for entry in win.file_tabs.values():
				self.assertEqual(entry.table_model.rowCount(), 4)
		finally:
			win.close()

	def test_tab_close_button_closes_correct_file(self):
		win = MainWindow()
		try:
			win.open_file(self.path)
			win.open_file(self.path)
			label_to_close = win._label_for_tab_index(0)
			win._on_tab_close_requested(0)
			self.assertNotIn(label_to_close, win.file_tabs)
			self.assertEqual(win.tabs_widget.count(), 1)
		finally:
			win.close()

	def test_drag_and_drop_opens_files(self):
		win = MainWindow()
		try:
			win._open_paths([self.path])
			self.assertEqual(len(win.file_tabs), 1)
			self.assertIn("sample.plflog", win.file_tabs)
		finally:
			win.close()


if __name__ == "__main__":
	unittest.main()
