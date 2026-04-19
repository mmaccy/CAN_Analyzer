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

### Large-file handling

Three layers work together for GB-class ASC files:

1. **`CanFrame` uses `@dataclass(slots=True)`** — no per-instance dict, ~50% smaller than a plain dataclass at 1M+ frames.
2. **`load_all_frames` drops `raw_line` and interns `frame_name`** — `raw_line` is only needed by `asc_writer`, which re-streams the source via `iter_frames` (the writer never sees cached frames). `sys.intern` deduplicates repeated frame names.
3. **`.asc.idx` cache** — `can_parser.asc_index` serializes `(schema_version, source_file_size, source_mtime_ns, header, frames)` as gzip+pickle next to the source. `load_index_if_valid` validates against current file metadata; `save_index` runs after a full parse. `MainWindow._load_asc_worker` tries the index first. Bump `INDEX_SCHEMA_VERSION` whenever `CanFrame` or `AscHeader` shape changes — stale pickles load as the wrong dataclass.

### Trace virtualization

`TracePanel` uses **page-windowed rendering**, not a single giant ListView. The Python→Flutter IPC serializes the full `controls` list for every `ListView`, so passing 600k+ row controls chokes the transport even if `item_extent` would virtualize the DOM. Instead, only the current page (`PAGE_SIZE = 2000`) is built as `Container` rows, fed into a `ft.ListView(item_extent=_ROW_HEIGHT)`, and swapped via `_render_current_page`. Within a page the fixed `item_extent` gives smooth scrolling with Flutter-side virtualization; between pages, `先頭/前/次/末尾` buttons or the Jump input switch pages. Header row is a separate `Container` above the ListView (not a `DataTable` because DataTable builds every row). Row click populates the bottom detail pane with decoded signals via `DbcLoader.decode_frame`. Jump input does `bisect_left` on timestamps → computes target page (`idx // PAGE_SIZE`) → switches page → `ListView.scroll_to(offset=offset_in_page * _ROW_HEIGHT)` for O(log n) seek.

### DBC decoding

`DbcLoader.decode_frame` iterates `msg.signals` and only emits a `SignalValue` if the signal name appears in `msg.decode()` output — this correctly filters out inactive multiplexed signals. Raw value is back-computed from physical via `scale`/`offset`. Both `.dbc` and `.arxml` are supported; extension selects the cantools loader.

## Flet version quirks

This codebase targets both legacy Flet (≤0.28) and current Flet (0.84+). Key compatibility shims:

- **FilePicker API**: Legacy uses `on_result` callbacks + `page.overlay`; new API returns awaitables and uses `page.services`. Selected via `_FLET_LEGACY = hasattr(ft, "FilePickerResultEvent")` in `main_window.py`.
- **PlotlyChart location**: `flet.plotly_chart` (legacy) vs `flet_charts.plotly_chart` (0.84+, separate `flet-charts` package). `graph_panel.py` tries both. `flet-charts` renders via `kaleido` SVG export — `kaleido>=1.0.0` is a required transitive dep.
- **No WebView on Windows desktop**: `flet-webview` does not support Windows desktop as of 0.84 (returns "Webview is not yet supported on this Platform"). The in-app graph is a static SVG (`PlotlyChart`). The "ブラウザで開く" button in `graph_panel.py` writes the Plotly self-contained HTML to a temp file and `webbrowser.open`s it so the user gets full Plotly interactivity (zoom/pan/reset/hover tooltips) in the default browser. The in-app SVG's non-functional modebar artifact is suppressed via `fig.update_layout(modebar=dict(remove=['all']))`.
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
