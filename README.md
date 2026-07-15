# Log Explorer

A [PyQt6](https://pypi.org/project/PyQt6/) desktop GUI for browsing, filtering and inspecting log files created by [pylogfile](https://pypi.org/project/pylogfile/) (`.plflog`, legacy `.hdf`, and `.json`).

Log Explorer covers the same ground as pylogfile's `lumber` CLI script, in a windowed form:

- Open one or more log files at once, either via **File > Open...** or by dragging files onto the window; each opened file gets its own closable tab with a sortable, scrollable log table (index, log level, timestamp, message, detail).
- A toggleable **Filter** panel, applied live across every open tab, to narrow the tables by minimum/maximum log level and message/detail search terms (match any or match all). The timestamp range fields auto-populate with the earliest/latest timestamp actually present across the open files, and a **Reset Range** button restores them after you've changed them (separate from **Clear Filters**, which turns filtering off entirely).
- A toggleable **Metadata** panel with the list of opened files, each file's log level definitions (including their pylogfile markdown color overrides), and general file info: an aggregate summary of log counts per level across all open files, plus a per-file breakdown (timespan, on-disk format details, file size/modified date).

## Installation

```
pip install -e .
```

## Usage

```
log-explorer [file1.plflog file2.plflog ...]
```

Or launch it with no arguments and use **File > Open Log File(s)...**.

## Requirements

- Python >= 3.9
- pylogfile >= 0.4.1
- PyQt6 >= 6.4.0
- h5py >= 3.11.0
