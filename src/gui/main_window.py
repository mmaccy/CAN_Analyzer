"""メインウィンドウ — レイアウト管理・ファイル I/O・スレッド管理"""

import threading
from pathlib import Path
from typing import List, Optional, Set, Tuple

import flet as ft

# Flet 0.84+ ではFilePicker API が同期リターン方式に変更
_FLET_LEGACY = hasattr(ft, "FilePickerResultEvent")

from models.can_frame import AscHeader, CanFrame
from models.app_config import AppConfig, CONFIG_FILE_EXTENSION, load_config, save_config
from can_parser.asc_parser import load_all_frames, parse_header
from can_parser.asc_writer import export_filtered
from can_parser.asc_index import load_index_if_valid, save_index
from can_parser.dbc_loader import DbcLoader
from gui.trace_panel import TracePanel
from gui.signal_tree_panel import SignalTreePanel
from gui.graph_panel import GraphPanel
from gui.statistics_panel import StatisticsPanel
from gui.connection_dialog import ConnectionDialog
from gui.statistics_panel import write_statistics_csv
from realtime.can_receiver import CanReceiver, ReceiverConfig
from utils.click_server import ChartClickServer, build_plotly_html
from utils.logger import get_logger, get_log_path


_log = get_logger(__name__)


class MainWindow:
    """CAN FD ログ解析ツール メインウィンドウ"""

    def __init__(self, page: ft.Page):
        self.page = page
        self.page.title = "CAN FD Log Analyzer"
        if hasattr(self.page, "window") and hasattr(self.page.window, "width"):
            self.page.window.width = 1400
            self.page.window.height = 900
        else:
            self.page.width = 1400
            self.page.height = 900

        self._frames: List[CanFrame] = []
        self._header: Optional[AscHeader] = None
        self._dbc_loader = DbcLoader()
        self._asc_path: Optional[str] = None

        # パネル
        self._trace_panel = TracePanel(on_frame_select=self._on_frame_selected)
        self._graph_panel = GraphPanel(
            on_request_png_save=self._request_graph_png_save,
            on_chart_time_clicked=self._on_graph_time_clicked,
            on_request_browser_open=self._open_chart_in_browser,
        )

        # ブラウザ Plotly クリック中継用 HTTP サーバ (遅延起動)
        self._click_server: Optional[ChartClickServer] = None
        self._click_poll_task = None
        self._signal_tree = SignalTreePanel(on_selection_changed=self._on_signal_selection_changed)
        self._statistics_panel = StatisticsPanel(on_request_csv_save=self._request_stats_csv_save)

        # PNG 保存用: 要求時に figure を一時保持
        self._pending_png_figure = None

        # プログレス
        self._progress_bar = ft.ProgressBar(visible=False, expand=True)
        self._progress_text = ft.Text("", size=11)

        # ファイルピッカー
        if _FLET_LEGACY:
            self._asc_picker = ft.FilePicker(on_result=self._on_asc_picked)
            self._dbc_picker = ft.FilePicker(on_result=self._on_dbc_picked)
            self._export_picker = ft.FilePicker(on_result=self._on_export_path_picked)
            self._export_db_picker = ft.FilePicker(on_result=self._on_export_db_path_picked)
            self._png_picker = ft.FilePicker(on_result=self._on_png_path_picked)
            self._config_save_picker = ft.FilePicker(on_result=self._on_config_save_picked)
            self._config_load_picker = ft.FilePicker(on_result=self._on_config_load_picked)
            self._custom_def_picker = ft.FilePicker(on_result=self._on_custom_def_picked)
            self._export_json_picker = ft.FilePicker(on_result=self._on_export_json_picked)
            self._export_dbc_picker = ft.FilePicker(on_result=self._on_export_dbc_picked)
            self._export_arxml_picker = ft.FilePicker(on_result=self._on_export_arxml_picked)
            self._stats_csv_picker = ft.FilePicker(on_result=self._on_stats_csv_picked)
            page.overlay.extend([
                self._asc_picker, self._dbc_picker, self._export_picker, self._export_db_picker,
                self._png_picker,
                self._config_save_picker, self._config_load_picker,
                self._custom_def_picker,
                self._export_json_picker, self._export_dbc_picker, self._export_arxml_picker,
                self._stats_csv_picker,
            ])
        else:
            self._asc_picker = ft.FilePicker()
            self._dbc_picker = ft.FilePicker()
            self._export_picker = ft.FilePicker()
            self._export_db_picker = ft.FilePicker()
            self._png_picker = ft.FilePicker()
            self._config_save_picker = ft.FilePicker()
            self._config_load_picker = ft.FilePicker()
            self._custom_def_picker = ft.FilePicker()
            self._export_json_picker = ft.FilePicker()
            self._export_dbc_picker = ft.FilePicker()
            self._export_arxml_picker = ft.FilePicker()
            self._stats_csv_picker = ft.FilePicker()
            page.services.extend([
                self._asc_picker, self._dbc_picker, self._export_picker, self._export_db_picker,
                self._png_picker,
                self._config_save_picker, self._config_load_picker,
                self._custom_def_picker,
                self._export_json_picker, self._export_dbc_picker, self._export_arxml_picker,
                self._stats_csv_picker,
            ])

        # ステータスバー
        self._status_file = ft.Text("ファイル未読込", size=11)
        self._status_frames = ft.Text("", size=11)
        self._status_time = ft.Text("", size=11)
        self._status_dbc = ft.Text("DB: なし", size=11)
        self._status_rt = ft.Text("接続: なし", size=11)

        # リアルタイム受信
        self._receiver: Optional[CanReceiver] = None
        self._last_rt_config: Optional[ReceiverConfig] = None
        self._rt_poll_task = None

        self._build_ui()
        self._setup_keyboard_shortcuts()
        self._setup_resize_tracking()
        page.update()

    def _build_ui(self) -> None:
        # ツールバー
        toolbar = ft.Row(
            controls=[
                ft.ElevatedButton(
                    "ASC 開く", icon=ft.Icons.FOLDER_OPEN,
                    on_click=self._on_open_asc,
                ),
                ft.ElevatedButton(
                    "DB 開く (DBC/ARXML)", icon=ft.Icons.DESCRIPTION,
                    on_click=self._on_open_dbc,
                ),
                ft.VerticalDivider(width=1),
                ft.ElevatedButton(
                    "エクスポート", icon=ft.Icons.SAVE_ALT,
                    on_click=self._on_export,
                    tooltip="トレースで現在表示中のフレームをエクスポート",
                ),
                ft.ElevatedButton(
                    "DB 抽出", icon=ft.Icons.FILTER_ALT,
                    on_click=self._on_export_db_only,
                    tooltip="読込済み DBC/ARXML に定義されているフレームのみを抜粋してエクスポート",
                ),
                ft.ElevatedButton(
                    "カスタム定義", icon=ft.Icons.EDIT_NOTE,
                    on_click=self._on_open_custom_def,
                    tooltip="カスタムシグナル定義 JSON を読込 (ARXML 未定義シグナルの追加・既存定義の上書き)",
                ),
                ft.PopupMenuButton(
                    content=ft.Row(
                        [ft.Icon(ft.Icons.IOS_SHARE, size=18), ft.Text("定義エクスポート", size=13)],
                        spacing=4,
                    ),
                    tooltip="現在のDB定義をエクスポート (JSON / DBC / ARXML)",
                    items=[
                        ft.PopupMenuItem(
                            content="JSON (カスタム定義形式)",
                            icon=ft.Icon(ft.Icons.DATA_OBJECT),
                            on_click=self._on_export_def_json,
                        ),
                        ft.PopupMenuItem(
                            content="DBC (Vector CANdb++形式)",
                            icon=ft.Icon(ft.Icons.DESCRIPTION),
                            on_click=self._on_export_def_dbc,
                        ),
                        ft.PopupMenuItem(
                            content="ARXML (AUTOSAR形式)",
                            icon=ft.Icon(ft.Icons.CODE),
                            on_click=self._on_export_def_arxml,
                        ),
                    ],
                ),
                ft.VerticalDivider(width=1),
                ft.ElevatedButton(
                    "設定保存", icon=ft.Icons.SAVE,
                    on_click=self._on_save_config,
                    tooltip="選択シグナル等の設定を .canalzcfg として保存",
                ),
                ft.ElevatedButton(
                    "設定読込", icon=ft.Icons.UPLOAD_FILE,
                    on_click=self._on_load_config,
                    tooltip=".canalzcfg を読み込んで選択シグナルを復元",
                ),
                ft.VerticalDivider(width=1),
                ft.ElevatedButton(
                    "接続", icon=ft.Icons.LINK,
                    on_click=self._on_connect,
                    tooltip="リアルタイム CAN 受信を開始 (Vector / Virtual)",
                ),
                ft.ElevatedButton(
                    "停止", icon=ft.Icons.LINK_OFF,
                    on_click=self._on_disconnect,
                    tooltip="リアルタイム受信を停止",
                ),
                ft.IconButton(
                    icon=ft.Icons.BUG_REPORT,
                    tooltip="ログファイルを既定アプリで開く",
                    on_click=self._on_open_log,
                ),
                ft.Container(expand=True),
                self._progress_bar,
                self._progress_text,
            ],
            spacing=8,
            alignment=ft.MainAxisAlignment.START,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        # タブボタン（ft.Tabs は Python 3.9 + Flet 0.28 で非互換のため手動実装）
        self._tab_buttons = []
        self._tab_panels = [self._trace_panel, self._graph_panel, self._statistics_panel]
        tab_labels = ["トレース", "グラフ", "統計"]
        for i, label in enumerate(tab_labels):
            btn = ft.ElevatedButton(
                label,
                data=i,
                on_click=self._on_tab_click,
                style=ft.ButtonStyle(
                    shape=ft.RoundedRectangleBorder(radius=0),
                    padding=ft.padding.symmetric(horizontal=20, vertical=10),
                ),
            )
            self._tab_buttons.append(btn)

        self._current_tab = 0
        tab_bar = ft.Row(controls=self._tab_buttons, spacing=0)

        # タブコンテンツ（visibility で切替）
        self._tab_content = ft.Stack(
            controls=[
                ft.Container(content=p, visible=(i == 0), expand=True)
                for i, p in enumerate(self._tab_panels)
            ],
            expand=True,
        )

        tab_area = ft.Column(
            controls=[tab_bar, self._tab_content],
            expand=True,
            spacing=0,
        )

        # 左パネル: シグナルツリー
        left_panel = ft.Container(
            content=self._signal_tree,
            width=300,
            border=ft.border.only(right=ft.BorderSide(1, ft.Colors.OUTLINE)),
        )

        # メインコンテンツ
        main_content = ft.Row(
            controls=[left_panel, tab_area],
            expand=True,
            spacing=0,
        )

        # ステータスバー
        status_bar = ft.Container(
            content=ft.Row(
                controls=[
                    self._status_file,
                    ft.VerticalDivider(width=1),
                    self._status_frames,
                    ft.VerticalDivider(width=1),
                    self._status_time,
                    ft.VerticalDivider(width=1),
                    self._status_dbc,
                    ft.VerticalDivider(width=1),
                    self._status_rt,
                ],
                spacing=12,
            ),
            padding=ft.padding.symmetric(horizontal=8, vertical=4),
            bgcolor=ft.Colors.SURFACE_TINT,
        )

        self.page.add(
            ft.Column(
                controls=[
                    ft.Container(content=toolbar, padding=ft.padding.symmetric(horizontal=8, vertical=4)),
                    ft.Divider(height=1),
                    main_content,
                    ft.Divider(height=1),
                    status_bar,
                ],
                expand=True,
                spacing=0,
            )
        )

    # ---------- File I/O ----------

    async def _on_open_asc(self, e) -> None:
        if _FLET_LEGACY:
            self._asc_picker.pick_files(
                dialog_title="ASC ファイルを開く",
                allowed_extensions=["asc"],
                file_type=ft.FilePickerFileType.CUSTOM,
            )
        else:
            files = await self._asc_picker.pick_files(
                dialog_title="ASC ファイルを開く",
                allowed_extensions=["asc"],
                file_type=ft.FilePickerFileType.CUSTOM,
            )
            if files:
                self._handle_asc_files(files)

    async def _on_open_dbc(self, e) -> None:
        if _FLET_LEGACY:
            self._dbc_picker.pick_files(
                dialog_title="データベースファイルを開く (DBC / ARXML)",
                allowed_extensions=["dbc", "arxml"],
                file_type=ft.FilePickerFileType.CUSTOM,
                allow_multiple=True,
            )
        else:
            files = await self._dbc_picker.pick_files(
                dialog_title="データベースファイルを開く (DBC / ARXML)",
                allowed_extensions=["dbc", "arxml"],
                file_type=ft.FilePickerFileType.CUSTOM,
                allow_multiple=True,
            )
            if files:
                self._handle_dbc_files(files)

    async def _on_export(self, e) -> None:
        if not self._asc_path:
            self._show_snackbar("先に ASC ファイルを読み込んでください")
            return
        if _FLET_LEGACY:
            self._export_picker.save_file(
                dialog_title="エクスポート先を指定",
                allowed_extensions=["asc"],
                file_type=ft.FilePickerFileType.CUSTOM,
                file_name="filtered_export.asc",
            )
        else:
            path = await self._export_picker.save_file(
                dialog_title="エクスポート先を指定",
                allowed_extensions=["asc"],
                file_type=ft.FilePickerFileType.CUSTOM,
                file_name="filtered_export.asc",
            )
            if path:
                self._handle_export_path(path)

    async def _on_export_db_only(self, e) -> None:
        """DB で定義されているフレームのみを抜粋してエクスポート"""
        if not self._asc_path:
            self._show_snackbar("先に ASC ファイルを読み込んでください")
            return
        if not self._dbc_loader.loaded_files:
            self._show_snackbar("先に DBC/ARXML を読み込んでください")
            return
        # 出力ファイル名のデフォルトは元 ASC 名 + _db_extract
        src_stem = Path(self._asc_path).stem
        default_name = f"{src_stem}_db_extract.asc"
        if _FLET_LEGACY:
            self._export_db_picker.save_file(
                dialog_title="DB 抽出の保存先を指定",
                allowed_extensions=["asc"],
                file_type=ft.FilePickerFileType.CUSTOM,
                file_name=default_name,
            )
        else:
            path = await self._export_db_picker.save_file(
                dialog_title="DB 抽出の保存先を指定",
                allowed_extensions=["asc"],
                file_type=ft.FilePickerFileType.CUSTOM,
                file_name=default_name,
            )
            if path:
                self._handle_export_db_path(path)

    def _on_asc_picked(self, e) -> None:
        """Legacy callback (Flet <=0.28)"""
        if not e.files:
            return
        self._handle_asc_files(e.files)

    def _handle_asc_files(self, files) -> None:
        """ASC ファイルピッカー結果の共通処理"""
        path = files[0].path
        self._asc_path = path
        self._status_file.value = Path(path).name
        self.page.update()

        # ワーカースレッドで読込
        self._progress_bar.visible = True
        self._progress_text.value = "読込中..."
        self.page.update()

        thread = threading.Thread(target=self._load_asc_worker, args=(path,), daemon=True)
        thread.start()

    def _load_asc_worker(self, path: str) -> None:
        """ワーカースレッド: ASC ファイル読込 (インデックス優先)"""
        import time

        def _throttled_progress(phase: str, last_ui_update: list):
            """0.3 秒ごとに間引いて UI 更新するコールバックを生成"""
            def _cb(done: int, total: int) -> None:
                now = time.monotonic()
                if now - last_ui_update[0] < 0.3 and done < total:
                    return
                last_ui_update[0] = now
                pct = (done / total) if total > 0 else 0
                self._progress_bar.value = pct
                if done >= total:
                    self._progress_text.value = f"{phase} 完了"
                else:
                    self._progress_text.value = f"{phase} {pct*100:.0f}% ({done:,}/{total:,})"
                try:
                    self.page.update()
                except RuntimeError:
                    pass
            return _cb

        try:
            # 1) 既存インデックスの利用を試行（GB 級でも秒で開く）
            idx_progress = _throttled_progress("インデックス読込中", [0.0])
            idx = load_index_if_valid(path, progress_callback=idx_progress)
            if idx is not None and idx.header is not None:
                self._header = idx.header
                self._frames = idx.frames
                if self._dbc_loader.loaded_files:
                    self._dbc_loader.resolve_frame_names(self._frames)
                self._on_asc_loaded()
                return

            # 2) テキストからフルパース
            self._header = parse_header(path)
            parse_cb = _throttled_progress("読込中", [0.0])

            def bytes_progress(read_bytes, total_bytes):
                # load_all_frames は (bytes, total_bytes) を渡してくるので
                # 件数ベースの throttled_progress と同じ関数で扱う。
                parse_cb(read_bytes, total_bytes)

            self._frames = load_all_frames(path, progress_callback=bytes_progress)

            # 3) インデックス保存（次回高速化）。失敗しても本処理は継続。
            save_cb = _throttled_progress("インデックス保存中", [0.0])
            save_index(path, self._header, self._frames, progress_callback=save_cb)

            # DBC でフレーム名解決
            if self._dbc_loader.loaded_files:
                self._dbc_loader.resolve_frame_names(self._frames)

            # UI 更新（メインスレッドへ）
            self._on_asc_loaded()
        except Exception as ex:
            self._progress_bar.visible = False
            self._progress_text.value = ""
            self._show_error(f"ASC 読込エラー: {ex}", exc=ex)
            self.page.update()

    def _on_asc_loaded(self) -> None:
        """ASC 読込完了後の UI 更新"""
        self._progress_bar.visible = False
        self._progress_text.value = ""

        self._trace_panel.set_frames(self._frames)
        self._trace_panel.set_dbc(self._dbc_loader)

        # シグナルツリーに存在フレーム ID を設定
        log_ids = set(f.arbitration_id for f in self._frames)
        self._signal_tree.set_log_frame_ids(log_ids)

        # グラフ・統計にデータ設定
        self._graph_panel.set_data(self._frames, self._dbc_loader)
        self._statistics_panel.set_frames(self._frames)

        # ステータス更新
        count = len(self._frames)
        self._status_frames.value = f"{count:,} frames"
        if count > 0:
            t0 = self._frames[0].timestamp
            t1 = self._frames[-1].timestamp
            self._status_time.value = f"{t0:.3f}s - {t1:.3f}s ({t1-t0:.3f}s)"
        else:
            self._status_time.value = ""

        self.page.update()

    def _on_dbc_picked(self, e) -> None:
        """Legacy callback (Flet <=0.28)"""
        if not e.files:
            return
        self._handle_dbc_files(e.files)

    def _handle_dbc_files(self, files) -> None:
        """DBC/ARXML ファイルピッカー結果の共通処理"""
        for f in files:
            try:
                self._dbc_loader.load_file(f.path)
            except Exception as ex:
                self._show_error(f"DB 読込エラー: {Path(f.path).name}: {ex}", exc=ex)

        # シグナルツリー更新
        self._signal_tree.set_dbc(self._dbc_loader)
        self._status_dbc.value = f"DB: {len(self._dbc_loader.loaded_files)} files"

        # 既にフレームが読込済みの場合、フレーム名を再解決
        if self._frames:
            self._dbc_loader.resolve_frame_names(self._frames)
            self._trace_panel.set_frames(self._frames)
            self._trace_panel.set_dbc(self._dbc_loader)
            self._graph_panel.set_data(self._frames, self._dbc_loader)
            log_ids = set(f.arbitration_id for f in self._frames)
            self._signal_tree.set_log_frame_ids(log_ids)
        else:
            # フレーム未読込でも DB 情報だけは先に反映しておく
            self._trace_panel.set_dbc(self._dbc_loader)

        self.page.update()

    # ---------- カスタムシグナル定義 ----------

    async def _on_open_custom_def(self, e) -> None:
        """カスタムシグナル定義 JSON を開く"""
        if _FLET_LEGACY:
            self._custom_def_picker.pick_files(
                dialog_title="カスタムシグナル定義 JSON を開く",
                allowed_extensions=["json"],
                file_type=ft.FilePickerFileType.CUSTOM,
                allow_multiple=True,
            )
        else:
            files = await self._custom_def_picker.pick_files(
                dialog_title="カスタムシグナル定義 JSON を開く",
                allowed_extensions=["json"],
                file_type=ft.FilePickerFileType.CUSTOM,
                allow_multiple=True,
            )
            if files:
                self._handle_custom_def_files(files)

    def _on_custom_def_picked(self, e) -> None:
        """Legacy callback (Flet <=0.28)"""
        if not e.files:
            return
        self._handle_custom_def_files(e.files)

    def _handle_custom_def_files(self, files) -> None:
        """カスタムシグナル定義ファイルの読込処理"""
        total_count = 0
        for f in files:
            try:
                count = self._dbc_loader.load_custom_file(f.path)
                total_count += count
                self._show_snackbar(
                    f"カスタム定義読込: {Path(f.path).name} ({count} メッセージ追加/更新)"
                )
            except Exception as ex:
                self._show_error(f"カスタム定義エラー: {Path(f.path).name}: {ex}", exc=ex)

        if total_count > 0:
            # シグナルツリー・トレース・グラフを更新
            self._signal_tree.set_dbc(self._dbc_loader)
            n_db = len(self._dbc_loader.loaded_files)
            n_custom = len(self._dbc_loader.custom_files)
            self._status_dbc.value = f"DB: {n_db} files + カスタム: {n_custom}"

            if self._frames:
                self._dbc_loader.resolve_frame_names(self._frames)
                self._trace_panel.set_frames(self._frames)
                self._trace_panel.set_dbc(self._dbc_loader)
                self._graph_panel.set_data(self._frames, self._dbc_loader)
                log_ids = set(f.arbitration_id for f in self._frames)
                self._signal_tree.set_log_frame_ids(log_ids)
            else:
                self._trace_panel.set_dbc(self._dbc_loader)

        self.page.update()

    # ── 定義エクスポート (JSON / DBC / ARXML) ──────────────

    async def _on_export_def_json(self, e) -> None:
        """定義エクスポート → JSON"""
        if not self._dbc_loader.loaded_files and not self._dbc_loader.custom_files:
            self._show_snackbar("DB が読み込まれていません")
            return
        if _FLET_LEGACY:
            self._export_json_picker.save_file(
                dialog_title="定義を JSON でエクスポート",
                allowed_extensions=["json"],
                file_type=ft.FilePickerFileType.CUSTOM,
                file_name="custom_definitions.json",
            )
        else:
            result = await self._export_json_picker.save_file(
                dialog_title="定義を JSON でエクスポート",
                allowed_extensions=["json"],
                file_type=ft.FilePickerFileType.CUSTOM,
                file_name="custom_definitions.json",
            )
            if result:
                self._handle_export_def_json(result)

    def _on_export_json_picked(self, e) -> None:
        """Legacy callback"""
        if not e.path:
            return
        self._handle_export_def_json(e.path)

    def _handle_export_def_json(self, path) -> None:
        output_path = path if isinstance(path, str) else path.path
        if not output_path.endswith(".json"):
            output_path += ".json"
        try:
            count = self._dbc_loader.export_all_json(output_path)
            self._show_snackbar(f"JSON エクスポート完了: {count} メッセージ → {Path(output_path).name}")
        except Exception as ex:
            self._show_error(f"JSON エクスポートエラー: {ex}", exc=ex)
        self.page.update()

    async def _on_export_def_dbc(self, e) -> None:
        """定義エクスポート → DBC"""
        if not self._dbc_loader.loaded_files and not self._dbc_loader.custom_files:
            self._show_snackbar("DB が読み込まれていません")
            return
        if _FLET_LEGACY:
            self._export_dbc_picker.save_file(
                dialog_title="定義を DBC でエクスポート",
                allowed_extensions=["dbc"],
                file_type=ft.FilePickerFileType.CUSTOM,
                file_name="exported.dbc",
            )
        else:
            result = await self._export_dbc_picker.save_file(
                dialog_title="定義を DBC でエクスポート",
                allowed_extensions=["dbc"],
                file_type=ft.FilePickerFileType.CUSTOM,
                file_name="exported.dbc",
            )
            if result:
                self._handle_export_def_dbc(result)

    def _on_export_dbc_picked(self, e) -> None:
        """Legacy callback"""
        if not e.path:
            return
        self._handle_export_def_dbc(e.path)

    def _handle_export_def_dbc(self, path) -> None:
        output_path = path if isinstance(path, str) else path.path
        if not output_path.endswith(".dbc"):
            output_path += ".dbc"
        try:
            count = self._dbc_loader.export_dbc(output_path)
            self._show_snackbar(f"DBC エクスポート完了: {count} メッセージ → {Path(output_path).name}")
        except Exception as ex:
            self._show_error(f"DBC エクスポートエラー: {ex}", exc=ex)
        self.page.update()

    async def _on_export_def_arxml(self, e) -> None:
        """定義エクスポート → ARXML"""
        if not self._dbc_loader.loaded_files and not self._dbc_loader.custom_files:
            self._show_snackbar("DB が読み込まれていません")
            return
        if _FLET_LEGACY:
            self._export_arxml_picker.save_file(
                dialog_title="定義を ARXML でエクスポート",
                allowed_extensions=["arxml"],
                file_type=ft.FilePickerFileType.CUSTOM,
                file_name="exported.arxml",
            )
        else:
            result = await self._export_arxml_picker.save_file(
                dialog_title="定義を ARXML でエクスポート",
                allowed_extensions=["arxml"],
                file_type=ft.FilePickerFileType.CUSTOM,
                file_name="exported.arxml",
            )
            if result:
                self._handle_export_def_arxml(result)

    def _on_export_arxml_picked(self, e) -> None:
        """Legacy callback"""
        if not e.path:
            return
        self._handle_export_def_arxml(e.path)

    def _handle_export_def_arxml(self, path) -> None:
        output_path = path if isinstance(path, str) else path.path
        if not output_path.endswith(".arxml"):
            output_path += ".arxml"
        try:
            count = self._dbc_loader.export_arxml(output_path)
            self._show_snackbar(f"ARXML エクスポート完了: {count} メッセージ → {Path(output_path).name}")
        except Exception as ex:
            self._show_error(f"ARXML エクスポートエラー: {ex}", exc=ex)
        self.page.update()

    def _on_export_path_picked(self, e) -> None:
        """Legacy callback (Flet <=0.28)"""
        if not e.path:
            return
        self._handle_export_path(e.path)

    def _on_export_db_path_picked(self, e) -> None:
        """Legacy callback for DB 抽出"""
        if not e.path:
            return
        self._handle_export_db_path(e.path)

    def _handle_export_path(self, output_path: str) -> None:
        """エクスポート先パスの共通処理（トレースフィルタベース）"""
        if not output_path.endswith(".asc"):
            output_path += ".asc"

        # 現在のトレースフィルタで表示中の ID を抽出対象にする
        filtered_ids = self._trace_panel.get_filtered_frame_ids()

        self._progress_bar.visible = True
        self._progress_text.value = "エクスポート中..."
        self.page.update()

        thread = threading.Thread(
            target=self._export_worker,
            args=(output_path, filtered_ids),
            daemon=True,
        )
        thread.start()

    def _handle_export_db_path(self, output_path: str) -> None:
        """DB 定義フレームのみエクスポート"""
        if not output_path.endswith(".asc"):
            output_path += ".asc"

        db_ids = self._dbc_loader.get_defined_frame_ids()
        if not db_ids:
            self._show_snackbar("DB 定義フレームが 0 件です")
            return

        self._progress_bar.visible = True
        self._progress_text.value = f"DB 抽出中... (対象 {len(db_ids):,} フレーム ID)"
        self.page.update()

        thread = threading.Thread(
            target=self._export_worker,
            args=(output_path, db_ids),
            daemon=True,
        )
        thread.start()

    def _export_worker(self, output_path: str, frame_ids: Set[int]) -> None:
        try:
            import time
            _last_ui_update = [0.0]

            def progress_cb(read_bytes, total_bytes):
                now = time.monotonic()
                if now - _last_ui_update[0] < 0.3:
                    return
                _last_ui_update[0] = now
                pct = read_bytes / total_bytes if total_bytes > 0 else 0
                self._progress_bar.value = pct
                self._progress_text.value = f"エクスポート中... {pct*100:.0f}%"
                try:
                    self.page.update()
                except RuntimeError:
                    pass

            count = export_filtered(
                source_path=self._asc_path,
                output_path=output_path,
                frame_ids=frame_ids if frame_ids else None,
                progress_callback=progress_cb,
            )
            self._progress_bar.visible = False
            self._progress_text.value = ""
            self._show_snackbar(f"エクスポート完了: {count:,} frames → {Path(output_path).name}")
            self.page.update()
        except Exception as ex:
            self._progress_bar.visible = False
            self._progress_text.value = ""
            self._show_error(f"エクスポートエラー: {ex}", exc=ex)
            self.page.update()

    # ---------- Graph PNG Save ----------

    def _request_graph_png_save(self, figure, signal_hint: Optional[str] = None) -> None:
        """GraphPanel からのコールバック: figure を保持して保存ダイアログを開く

        単一シグナル選択時は `[ASC名]_[シグナル名].png`、
        それ以外は `[ASC名]_graph.png` を既定ファイル名とする。
        """
        self._pending_png_figure = figure
        default_name = self._build_png_default_name(signal_hint)

        if _FLET_LEGACY:
            self._png_picker.save_file(
                dialog_title="PNG 保存先",
                allowed_extensions=["png"],
                file_type=ft.FilePickerFileType.CUSTOM,
                file_name=default_name,
            )
        else:
            # 非 legacy は save_file が coroutine のため page.run_task 経由で実行
            async def _pick():
                path = await self._png_picker.save_file(
                    dialog_title="PNG 保存先",
                    allowed_extensions=["png"],
                    file_type=ft.FilePickerFileType.CUSTOM,
                    file_name=default_name,
                )
                if path:
                    self._handle_png_save_path(path)

            try:
                self.page.run_task(_pick)
            except (AttributeError, RuntimeError):
                pass

    def _build_png_default_name(self, signal_hint: Optional[str]) -> str:
        """PNG 保存時の既定ファイル名を生成する"""
        stem = Path(self._asc_path).stem if self._asc_path else "graph"
        if signal_hint:
            # ファイル名として安全な文字に限定（空白や記号を削らずそのまま使う）
            return f"{stem}_{signal_hint}.png"
        return f"{stem}_graph.png"

    def _on_png_path_picked(self, e) -> None:
        """Legacy callback"""
        if not e.path:
            return
        self._handle_png_save_path(e.path)

    def _handle_png_save_path(self, output_path: str) -> None:
        """保存先が確定した時の共通処理"""
        if not output_path.lower().endswith(".png"):
            output_path += ".png"
        self._pending_png_figure = None
        try:
            # kaleido が Python 3.13 でハングするため matplotlib で PNG を生成
            png_bytes = self._graph_panel.render_png_bytes_for_save(dpi=200)
            if png_bytes is None:
                self._show_snackbar("保存対象のグラフがありません")
                return
            with open(output_path, "wb") as f:
                f.write(png_bytes)
            self._show_snackbar(f"PNG 保存完了: {Path(output_path).name}")
        except Exception as ex:
            self._show_error(f"PNG 保存エラー: {ex}", exc=ex)
        self.page.update()

    # ---------- Config Save / Load ----------

    async def _on_save_config(self, e) -> None:
        if _FLET_LEGACY:
            self._config_save_picker.save_file(
                dialog_title="設定ファイル保存",
                allowed_extensions=[CONFIG_FILE_EXTENSION],
                file_type=ft.FilePickerFileType.CUSTOM,
                file_name=f"settings.{CONFIG_FILE_EXTENSION}",
            )
        else:
            path = await self._config_save_picker.save_file(
                dialog_title="設定ファイル保存",
                allowed_extensions=[CONFIG_FILE_EXTENSION],
                file_type=ft.FilePickerFileType.CUSTOM,
                file_name=f"settings.{CONFIG_FILE_EXTENSION}",
            )
            if path:
                self._handle_config_save(path)

    async def _on_load_config(self, e) -> None:
        if _FLET_LEGACY:
            self._config_load_picker.pick_files(
                dialog_title="設定ファイル読込",
                allowed_extensions=[CONFIG_FILE_EXTENSION],
                file_type=ft.FilePickerFileType.CUSTOM,
            )
        else:
            files = await self._config_load_picker.pick_files(
                dialog_title="設定ファイル読込",
                allowed_extensions=[CONFIG_FILE_EXTENSION],
                file_type=ft.FilePickerFileType.CUSTOM,
            )
            if files:
                self._handle_config_load(files[0].path)

    def _on_config_save_picked(self, e) -> None:
        """Legacy callback"""
        if not e.path:
            return
        self._handle_config_save(e.path)

    def _on_config_load_picked(self, e) -> None:
        """Legacy callback"""
        if not e.files:
            return
        self._handle_config_load(e.files[0].path)

    def _handle_config_save(self, output_path: str) -> None:
        if not output_path.endswith(f".{CONFIG_FILE_EXTENSION}"):
            output_path += f".{CONFIG_FILE_EXTENSION}"
        try:
            cfg = AppConfig(selected_signals=self._signal_tree.get_selected_signals())
            save_config(cfg, output_path)
            self._show_snackbar(f"設定を保存しました: {Path(output_path).name}")
        except Exception as ex:
            self._show_error(f"設定保存エラー: {ex}", exc=ex)

    def _handle_config_load(self, input_path: str) -> None:
        try:
            cfg = load_config(input_path)
        except Exception as ex:
            self._show_error(f"設定読込エラー: {ex}", exc=ex)
            return
        if not self._dbc_loader.loaded_files:
            self._show_snackbar("先に DBC/ARXML を読み込んでください（設定のシグナル名は DBC 基準）")
            return
        self._signal_tree.set_selected_signals(cfg.selected_signals)
        self._show_snackbar(
            f"設定を読み込みました: {len(cfg.selected_signals)} 件のシグナル選択を復元"
        )
        self.page.update()

    # ---------- Callbacks ----------

    def _on_tab_click(self, e) -> None:
        """タブ切替"""
        idx = e.control.data
        self._current_tab = idx
        for i, container in enumerate(self._tab_content.controls):
            container.visible = (i == idx)
        self.page.update()

    def _on_frame_selected(self, frame: CanFrame) -> None:
        """トレースでフレームが選択された時にグラフへカーソル位置を反映"""
        if frame is None:
            self._graph_panel.set_cursor_time(None)
            return
        self._graph_panel.set_cursor_time(frame.timestamp)

    def _on_graph_time_clicked(self, t: float) -> None:
        """グラフ (アプリ内 SVG / ブラウザ Plotly) でクリックされた時刻に
        トレースをジャンプさせ、グラフ側にもカーソル線を表示する"""
        # トレースを該当時刻にジャンプ (二分探索ベース)
        try:
            self._trace_panel.jump_to_time(t)
        except Exception as ex:
            _log.exception("トレースジャンプ失敗 (t=%.6f): %s", t, ex)
        # グラフカーソルにも反映
        self._graph_panel.set_cursor_time(t)
        # トレースタブに自動で切り替え (クリック→ジャンプの流れを直感的にする)
        if self._current_tab != 0:
            self._switch_tab(0)
        else:
            self.page.update()

    def _open_chart_in_browser(self, figure) -> Optional[str]:
        """GraphPanel から呼ばれる: ローカル HTTP サーバに HTML を登録して URL を返す

        Plan B: ブラウザ Plotly のクリックを Flet に中継するため、自前サーバ経由で
        HTML をホストする。サーバは初回のみ起動し、以降は HTML 差し替えで再利用。
        """
        try:
            if self._click_server is None:
                self._click_server = ChartClickServer()
                self._click_server.start()
                self._start_click_poll()
            # Plotly HTML にクリック中継 JS を埋め込む
            from gui.graph_panel import _BROWSER_PLOTLY_CONFIG
            html = build_plotly_html(figure, plotly_config=_BROWSER_PLOTLY_CONFIG)
            self._click_server.set_html(html)
            return self._click_server.url
        except Exception as ex:
            _log.exception("ブラウザサーバ起動失敗: %s", ex)
            return None

    def _start_click_poll(self) -> None:
        """ブラウザクリックを 200ms ごとにドレインしてジャンプに転送"""
        import asyncio

        async def _poll():
            while self._click_server is not None:
                try:
                    times = self._click_server.drain_clicks()
                    for t in times:
                        self._on_graph_time_clicked(t)
                except Exception:
                    _log.exception("クリックポーリングで例外")
                await asyncio.sleep(0.2)

        try:
            self._click_poll_task = self.page.run_task(_poll)
        except (AttributeError, RuntimeError):
            # run_task 不可環境: スレッドフォールバック
            import threading
            import time as _time

            def _thread_poll():
                while self._click_server is not None:
                    try:
                        times = self._click_server.drain_clicks()
                        for t in times:
                            self._on_graph_time_clicked(t)
                    except Exception:
                        _log.exception("クリックポーリングで例外 (スレッド版)")
                    _time.sleep(0.2)

            threading.Thread(target=_thread_poll, daemon=True).start()

    def _setup_resize_tracking(self) -> None:
        """ウィンドウリサイズを GraphPanel に伝播 (クリック幅の正規化に使う)"""
        # 左パネル(300) + 区切り(1) + 余白を引いた値が概ねグラフ表示幅
        _LEFT_PANEL_AND_PADDING = 320

        def _update(width: float) -> None:
            chart_w = max(width - _LEFT_PANEL_AND_PADDING, 200.0)
            self._graph_panel.notify_chart_width(chart_w)

        # 初期サイズ
        try:
            init_w = float(getattr(self.page, "width", 0) or 0)
            if init_w > 0:
                _update(init_w)
        except Exception:
            pass

        def _on_resized(e) -> None:
            try:
                _update(float(getattr(e, "width", 0) or 0))
            except Exception:
                pass

        self.page.on_resized = _on_resized

    def _on_open_log(self, e=None) -> None:
        """ログファイルを既定アプリで開く"""
        path = get_log_path()
        if path is None or not path.exists():
            self._show_snackbar("ログファイルがまだ存在しません")
            return
        try:
            import os
            if hasattr(os, "startfile"):
                os.startfile(str(path))  # type: ignore[attr-defined]
            else:
                import webbrowser
                webbrowser.open(path.as_uri())
        except Exception as ex:
            self._show_error(f"ログファイルを開けませんでした: {ex}", exc=ex)

    # ---------- Keyboard Shortcuts ----------

    def _setup_keyboard_shortcuts(self) -> None:
        """グローバルキーボードショートカットを登録する

        対応:
          Ctrl+O         : ASC を開く
          Ctrl+D         : DB (DBC/ARXML) を開く
          Ctrl+E         : エクスポート
          Ctrl+Shift+E   : DB 抽出
          Ctrl+S         : 設定保存
          Ctrl+L         : 設定読込
          Ctrl+K         : 接続
          Ctrl+Shift+K   : 停止
          Ctrl+1 / 2 / 3 : タブ切替 (トレース / グラフ / 統計)
          F5             : 統計を更新 (統計タブ表示中のみ)
        """
        self.page.on_keyboard_event = self._on_keyboard_event

    def _on_keyboard_event(self, e) -> None:
        """KeyboardEvent ハンドラ。修飾キー + キーラベルで分岐する"""
        key = (getattr(e, "key", "") or "").upper()
        ctrl = bool(getattr(e, "ctrl", False))
        shift = bool(getattr(e, "shift", False))

        # F キー類（修飾なし）
        if not ctrl and not shift:
            if key == "F5":
                # 統計タブ表示中なら更新
                if self._current_tab == 2:
                    self._statistics_panel.refresh()
                    try:
                        self._statistics_panel.update()
                    except (AssertionError, RuntimeError):
                        pass
            return

        # Ctrl + 何か
        if ctrl and not shift:
            handlers_async = {
                "O": self._on_open_asc,
                "D": self._on_open_dbc,
                "E": self._on_export,
                "S": self._on_save_config,
                "L": self._on_load_config,
                "K": self._on_connect_async,
            }
            if key in handlers_async:
                self._dispatch(handlers_async[key])
                return
            # タブ切替 (Ctrl+1/2/3)
            tab_map = {"1": 0, "2": 1, "3": 2}
            if key in tab_map:
                self._switch_tab(tab_map[key])
                return

        # Ctrl+Shift + 何か
        if ctrl and shift:
            if key == "E":
                self._dispatch(self._on_export_db_only)
                return
            if key == "K":
                # 停止は同期ハンドラ
                self._on_disconnect(None)
                return

    async def _on_connect_async(self, e=None) -> None:
        """Ctrl+K 用: 既存の sync ハンドラ _on_connect を呼ぶラッパ

        on_keyboard_event は sync だが他のメニューハンドラと統一するため async で包む。
        """
        self._on_connect(None)

    def _dispatch(self, handler) -> None:
        """ハンドラが async なら page.run_task で起動、sync ならその場で呼ぶ"""
        import asyncio
        try:
            if asyncio.iscoroutinefunction(handler):
                self.page.run_task(handler, None)
            else:
                handler(None)
        except (AttributeError, RuntimeError):
            # run_task 不可環境のフォールバック
            try:
                handler(None)
            except Exception:
                pass

    def _switch_tab(self, idx: int) -> None:
        """タブをプログラム的に切り替える (キーボードショートカット用)"""
        if not (0 <= idx < len(self._tab_content.controls)):
            return
        self._current_tab = idx
        for i, container in enumerate(self._tab_content.controls):
            container.visible = (i == idx)
        self.page.update()

    # ---------- Statistics CSV Save ----------

    def _request_stats_csv_save(self) -> None:
        """統計パネルから呼ばれる: CSV 保存ダイアログを開く"""
        if not self._frames:
            self._show_snackbar("先に ASC ファイルを読み込んでください")
            return
        stem = Path(self._asc_path).stem if self._asc_path else "statistics"
        default_name = f"{stem}_statistics.csv"
        if _FLET_LEGACY:
            self._stats_csv_picker.save_file(
                dialog_title="統計 CSV 保存先",
                allowed_extensions=["csv"],
                file_type=ft.FilePickerFileType.CUSTOM,
                file_name=default_name,
            )
        else:
            async def _pick():
                path = await self._stats_csv_picker.save_file(
                    dialog_title="統計 CSV 保存先",
                    allowed_extensions=["csv"],
                    file_type=ft.FilePickerFileType.CUSTOM,
                    file_name=default_name,
                )
                if path:
                    self._handle_stats_csv_save(path if isinstance(path, str) else path.path)

            try:
                self.page.run_task(_pick)
            except (AttributeError, RuntimeError):
                pass

    def _on_stats_csv_picked(self, e) -> None:
        """Legacy callback"""
        if not e.path:
            return
        self._handle_stats_csv_save(e.path)

    def _handle_stats_csv_save(self, output_path: str) -> None:
        if not output_path.lower().endswith(".csv"):
            output_path += ".csv"
        try:
            stats = self._statistics_panel.get_stats()
            if not stats:
                self._show_snackbar("統計データがありません。「統計を更新」を押してから再試行してください")
                return
            count = write_statistics_csv(stats, output_path)
            self._show_snackbar(f"CSV エクスポート完了: {count} 行 → {Path(output_path).name}")
        except Exception as ex:
            self._show_error(f"CSV エクスポートエラー: {ex}", exc=ex)
        self.page.update()

    # ---------- Realtime CAN 受信 ----------

    def _on_connect(self, e) -> None:
        """接続ボタン: 接続設定ダイアログを開く"""
        if self._receiver is not None and self._receiver.is_running:
            self._show_snackbar("既に接続中です。先に「停止」してください")
            return
        dialog = ConnectionDialog(
            self.page,
            on_submit=self._start_receiver,
            initial=self._last_rt_config,
        )
        dialog.open()

    def _on_disconnect(self, e) -> None:
        """停止ボタン: 受信を停止する"""
        if self._receiver is None:
            return
        rx = self._receiver
        self._receiver = None  # ポーリングループに停止を通知
        try:
            rx.stop()
        except Exception as ex:
            self._show_error(f"停止エラー: {ex}", exc=ex)
        stats = rx.get_stats()
        log_msg = ""
        if self._last_rt_config and self._last_rt_config.log_path:
            log_msg = f" / ログ: {Path(self._last_rt_config.log_path).name}"
        self._status_rt.value = f"接続: なし (前回 {stats.rx_count:,} 受信)"
        self._show_snackbar(
            f"受信停止: {stats.rx_count:,} 受信 / {stats.dropped_count:,} drop / "
            f"{stats.error_count:,} err{log_msg}"
        )
        self.page.update()

    def _start_receiver(self, config: ReceiverConfig) -> None:
        """ConnectionDialog から呼ばれる: 受信を開始"""
        self._last_rt_config = config
        try:
            rx = CanReceiver(config)
            rx.start()
        except Exception as ex:
            self._show_error(f"接続エラー: {ex}", exc=ex)
            return
        self._receiver = rx
        # ASC ファイルが未読込でも live 受信表示はトレースに流すため、
        # 既存フレームが空ならパネル側を初期化しておく。
        if not self._frames:
            self._frames = []
            self._trace_panel.set_frames(self._frames)
            self._trace_panel.set_dbc(self._dbc_loader)

        ch_str = ",".join(str(c) for c in config.channels) if config.channels else "?"
        self._status_rt.value = (
            f"接続中: {config.interface} ch={ch_str}"
            f"{' (FD)' if config.fd else ''}"
        )
        self.page.update()
        # ポーリング開始
        self._schedule_rt_poll()

    def _schedule_rt_poll(self) -> None:
        """200ms ごとに受信キューをドレインして UI に流す。グラフ再描画は 1Hz に間引く。"""
        import asyncio
        import time as _time

        # グラフ再描画スロットル: 1Hz (matplotlib SVG 生成は重いため)
        _GRAPH_REFRESH_INTERVAL = 1.0
        last_graph_refresh = [0.0]

        def _process_batch(new_frames: List[CanFrame]) -> None:
            """受信バッチに対する共通処理（async / threaded どちらからも呼ぶ）"""
            if self._dbc_loader.loaded_files:
                self._dbc_loader.resolve_frame_names(new_frames)
            self._frames.extend(new_frames)
            self._trace_panel.add_frames(new_frames)
            self._graph_panel.add_frames(new_frames)
            stats = self._receiver.get_stats()
            self._status_frames.value = f"{len(self._frames):,} frames"
            self._status_rt.value = (
                f"接続中: rx={stats.rx_count:,} / drop={stats.dropped_count:,}"
            )

            # グラフ再描画は throttle
            now = _time.monotonic()
            if now - last_graph_refresh[0] >= _GRAPH_REFRESH_INTERVAL:
                last_graph_refresh[0] = now
                self._graph_panel.refresh_live()

            try:
                self.page.update()
            except RuntimeError:
                pass

        async def _poll_loop():
            while self._receiver is not None and self._receiver.is_running:
                try:
                    new_frames = self._receiver.drain(max_items=2000)
                    if new_frames:
                        _process_batch(new_frames)
                except Exception:
                    _log.exception("RT ポーリングループで例外")
                await asyncio.sleep(0.2)

        try:
            self._rt_poll_task = self.page.run_task(_poll_loop)
        except (AttributeError, RuntimeError):
            # run_task 未対応環境ではフォールバックでスレッドポーリング
            import threading

            def _thread_poll():
                while self._receiver is not None and self._receiver.is_running:
                    try:
                        new_frames = self._receiver.drain(max_items=2000)
                        if new_frames:
                            _process_batch(new_frames)
                    except Exception:
                        _log.exception("RT ポーリングループで例外 (スレッド版)")
                    _time.sleep(0.2)

            threading.Thread(target=_thread_poll, daemon=True).start()

    def _on_signal_selection_changed(self, selected: List[Tuple[int, str]]) -> None:
        """シグナル選択が変更された時"""
        self._graph_panel.update_signals(selected)

    # ---------- Helpers ----------

    def _show_snackbar(self, message: str) -> None:
        sb = ft.SnackBar(ft.Text(message), duration=4000)
        if hasattr(self.page, "snack_bar"):
            self.page.snack_bar = sb
            self.page.snack_bar.open = True
        else:
            self.page.show_dialog(sb)

    def _show_error(self, user_message: str, exc: Optional[BaseException] = None) -> None:
        """ユーザにエラー通知 + ログにスタックトレースを残す統一ヘルパ

        Args:
            user_message: ユーザに表示する短いメッセージ（日本語可）
            exc: 元例外。指定するとログに `logger.exception` でスタックトレースを残す
        """
        if exc is not None:
            _log.error("%s: %s", user_message, exc, exc_info=exc)
        else:
            _log.error("%s", user_message)
        log_hint = ""
        log_path = get_log_path()
        if log_path is not None:
            log_hint = f"  (詳細: {log_path})"
        self._show_snackbar(f"{user_message}{log_hint}")
