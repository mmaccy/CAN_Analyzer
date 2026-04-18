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
from can_parser.dbc_loader import DbcLoader
from gui.trace_panel import TracePanel
from gui.signal_tree_panel import SignalTreePanel
from gui.graph_panel import GraphPanel
from gui.statistics_panel import StatisticsPanel


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
        self._graph_panel = GraphPanel()
        self._signal_tree = SignalTreePanel(on_selection_changed=self._on_signal_selection_changed)
        self._statistics_panel = StatisticsPanel()

        # プログレス
        self._progress_bar = ft.ProgressBar(visible=False, expand=True)
        self._progress_text = ft.Text("", size=11)

        # ファイルピッカー
        if _FLET_LEGACY:
            self._asc_picker = ft.FilePicker(on_result=self._on_asc_picked)
            self._dbc_picker = ft.FilePicker(on_result=self._on_dbc_picked)
            self._export_picker = ft.FilePicker(on_result=self._on_export_path_picked)
            self._config_save_picker = ft.FilePicker(on_result=self._on_config_save_picked)
            self._config_load_picker = ft.FilePicker(on_result=self._on_config_load_picked)
            page.overlay.extend([
                self._asc_picker, self._dbc_picker, self._export_picker,
                self._config_save_picker, self._config_load_picker,
            ])
        else:
            self._asc_picker = ft.FilePicker()
            self._dbc_picker = ft.FilePicker()
            self._export_picker = ft.FilePicker()
            self._config_save_picker = ft.FilePicker()
            self._config_load_picker = ft.FilePicker()
            page.services.extend([
                self._asc_picker, self._dbc_picker, self._export_picker,
                self._config_save_picker, self._config_load_picker,
            ])

        # ステータスバー
        self._status_file = ft.Text("ファイル未読込", size=11)
        self._status_frames = ft.Text("", size=11)
        self._status_time = ft.Text("", size=11)
        self._status_dbc = ft.Text("DB: なし", size=11)

        self._build_ui()
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
        """ワーカースレッド: ASC ファイル読込"""
        try:
            self._header = parse_header(path)

            import time
            _last_ui_update = [0.0]

            def progress_cb(read_bytes, total_bytes):
                now = time.monotonic()
                # UI 更新は最大 0.3 秒ごとに抑制
                if now - _last_ui_update[0] < 0.3:
                    return
                _last_ui_update[0] = now
                pct = read_bytes / total_bytes if total_bytes > 0 else 0
                self._progress_bar.value = pct
                self._progress_text.value = f"読込中... {pct*100:.0f}%"
                try:
                    self.page.update()
                except RuntimeError:
                    pass  # dictionary changed size — 無視して続行

            self._frames = load_all_frames(path, progress_callback=progress_cb)

            # DBC でフレーム名解決
            if self._dbc_loader.loaded_files:
                self._dbc_loader.resolve_frame_names(self._frames)

            # UI 更新（メインスレッドへ）
            self._on_asc_loaded()
        except Exception as ex:
            self._progress_bar.visible = False
            self._progress_text.value = ""
            self._show_snackbar(f"ASC 読込エラー: {ex}")
            self.page.update()

    def _on_asc_loaded(self) -> None:
        """ASC 読込完了後の UI 更新"""
        self._progress_bar.visible = False
        self._progress_text.value = ""

        self._trace_panel.set_frames(self._frames)

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
                self._show_snackbar(f"DB 読込エラー: {Path(f.path).name}: {ex}")

        # シグナルツリー更新
        self._signal_tree.set_dbc(self._dbc_loader)
        self._status_dbc.value = f"DB: {len(self._dbc_loader.loaded_files)} files"

        # 既にフレームが読込済みの場合、フレーム名を再解決
        if self._frames:
            self._dbc_loader.resolve_frame_names(self._frames)
            self._trace_panel.set_frames(self._frames)
            self._graph_panel.set_data(self._frames, self._dbc_loader)
            log_ids = set(f.arbitration_id for f in self._frames)
            self._signal_tree.set_log_frame_ids(log_ids)

        self.page.update()

    def _on_export_path_picked(self, e) -> None:
        """Legacy callback (Flet <=0.28)"""
        if not e.path:
            return
        self._handle_export_path(e.path)

    def _handle_export_path(self, output_path: str) -> None:
        """エクスポート先パスの共通処理"""
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
            self._show_snackbar(f"エクスポートエラー: {ex}")
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
            self._show_snackbar(f"設定保存エラー: {ex}")

    def _handle_config_load(self, input_path: str) -> None:
        try:
            cfg = load_config(input_path)
        except Exception as ex:
            self._show_snackbar(f"設定読込エラー: {ex}")
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
        """トレースでフレームが選択された時"""
        pass

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
