# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

All commands assume the venv is activated: `source .venv/Scripts/activate` (Git Bash) or `.venv\Scripts\activate` (cmd/PowerShell).

- Run the app: `python src/main.py`
- Run all tests: `pytest tests/`
- Run a single test: `pytest tests/test_asc_parser.py::test_parse -v`
- Install deps: `pip install -r requirements.txt`

Venv was created with Python 3.14 (`py -3.14 -m venv .venv`). `src/main.py` prepends `src/` to `sys.path`, so imports inside `src/` use bare module paths (`from gui.main_window import MainWindow`, not `from src.gui...`). Tests replicate this via `sys.path.insert` at the top of each test file.

## Architecture

### Single-page Flet app with tab-based panels

`main.py` → `gui.main_window.MainWindow` owns the page and composes four panels that communicate through callbacks on the window:

- `TracePanel` — frame table with filtering/pagination
- `GraphPanel` — Plotly time-series chart (overlay or subplot mode)
- `StatisticsPanel` — per-frame cycle stats + bus load
- `SignalTreePanel` — DBC frame/signal tree with checkboxes driving `GraphPanel`

The signal tree is rendered to the left of a tab container holding the other three panels. Tab switching is a manual `visible` toggle on a `Stack` (not `ft.Tabs`) — `main_window._on_tab_click` drives this.

### Data flow

1. User picks ASC → `_handle_asc_files` → worker thread parses via `can_parser.asc_parser.load_all_frames` with progress callback → `_on_asc_loaded` pushes frames into each panel.
2. User picks DBC/ARXML → `DbcLoader.load_file` accumulates into a single `cantools.database.Database` (multi-file merge). Tree rebuilds; existing frames get `frame_name` re-resolved.
3. User checks signals → `SignalTreePanel` callback → `GraphPanel.update_signals` → rebuild Plotly figure → `Container.content` swap (see Flet quirks below).
4. Export: filtered frame IDs come from `TracePanel.get_filtered_frame_ids()`; `asc_writer.export_filtered` re-streams the source file line-by-line (does not round-trip parsed frames) to preserve original formatting.

### Threading

Long-running I/O (ASC parse, export) runs in `threading.Thread` workers that call `page.update()` directly. Progress callbacks self-throttle to 0.3s and wrap `page.update()` in `try/except RuntimeError` to tolerate concurrent dict mutation during updates.

### ASC parser

`can_parser.asc_parser` streams line-by-line (never loads the whole file). Two regexes handle Classic CAN and CAN FD; field widths vary in Vector's CANoe 17.x output so patterns use `\s*` aggressively. Extended IDs are marked with a trailing `x` on the hex ID. CAN FD DLC codes 9–15 map to 12/16/20/24/32/48/64 bytes (see `DLC_TO_LENGTH` in `models/can_frame.py`). Format reference is in `docs/SPEC_CAN_Tool.md` §6.2.

### DBC decoding

`DbcLoader.decode_frame` iterates `msg.signals` and only emits a `SignalValue` if the signal name appears in `msg.decode()` output — this correctly filters out inactive multiplexed signals. Raw value is back-computed from physical via `scale`/`offset`. Both `.dbc` and `.arxml` are supported; extension selects the cantools loader.

## Flet version quirks

This codebase targets both legacy Flet (≤0.28) and current Flet (0.84+). Key compatibility shims:

- **FilePicker API**: Legacy uses `on_result` callbacks + `page.overlay`; new API returns awaitables and uses `page.services`. Selected via `_FLET_LEGACY = hasattr(ft, "FilePickerResultEvent")` in `main_window.py`.
- **PlotlyChart location**: `flet.plotly_chart` (legacy) vs `flet_charts.plotly_chart` (0.84+, separate `flet-charts` package). `graph_panel.py` tries both. `flet-charts` renders via `kaleido` SVG export — `kaleido>=1.0.0` is a required transitive dep.
- **ExpansionTile kwarg**: `expanded` vs `initially_expanded` — `signal_tree_panel.py` introspects `__init__.__code__.co_varnames` at runtime.
- **Chart refresh**: When selected signals change, `GraphPanel` creates a *new* `PlotlyChart` and swaps it into a `Container.content` (not `Column.controls` slot replacement). Flet 0.84's `object_patch` diff does not reliably re-trigger SVG regeneration on slot replacement — the container swap does.

## Plot rendering

`analysis.graph_builder` uses `go.Scatter` with `line.shape="hv"` (step function) — CAN signals are discrete so linear interpolation misrepresents transitions. `Scattergl` is avoided because kaleido's SVG export loses fidelity on WebGL traces. Values are sorted by timestamp before plotting as a safety net.

## Platform notes

Windows-only due to Vector XL Driver dependency for the (not-yet-implemented) realtime receive path in `src/realtime/`. Current code is Phase 2 — realtime is stubbed out.

## Specification

`docs/SPEC_CAN_Tool.md` is the authoritative spec (Japanese). `TASKS.md` (at repo root) tracks phased implementation. UI labels, docstrings, and in-code comments are in Japanese — preserve language when editing.

### Spec-Code Sync

`docs/SPEC_CAN_Tool.md` is the source of truth for features. When implementing work that is not described in the spec (e.g., from verbal user instructions), update the spec in the same change so it stays consistent with the code.

### Task Management

Before starting implementation, break the work into phases and individual tasks in `TASKS.md`. As implementation progresses, update task status — mark completed items and add new tasks discovered mid-development.
