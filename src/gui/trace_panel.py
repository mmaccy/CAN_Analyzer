"""トレースパネル — CANoe Trace 風フレーム一覧表示

フィルタバー + DataTable によるフレーム一覧。
"""

import flet as ft
from typing import Callable, List, Optional, Set

from models.can_frame import CanFrame

# Flet 0.84+ Dropdown: on_change → on_select
_DD_EVENT = "on_select" if "on_select" in ft.Dropdown.__init__.__code__.co_varnames else "on_change"
# Flet 0.84+ DataRow: on_select_changed → on_select_change
_DR_EVENT = "on_select_change" if "on_select_change" in ft.DataRow.__init__.__code__.co_varnames else "on_select_changed"

# 1ページあたりの表示行数（仮想スクロール代替）
PAGE_SIZE = 500


class TracePanel(ft.Column):
    """トレースビュー: フレーム一覧テーブル + フィルタ"""

    def __init__(self, on_frame_select: Optional[Callable] = None):
        super().__init__(expand=True, spacing=0)
        self._all_frames: List[CanFrame] = []
        self._filtered_frames: List[CanFrame] = []
        self._on_frame_select = on_frame_select
        self._page_index = 0

        # フィルタ UI
        self._search_field = ft.TextField(
            label="検索 (ID / フレーム名)",
            width=250,
            dense=True,
            on_submit=self._on_filter_changed,
            on_change=self._on_filter_changed,
        )
        self._channel_dropdown = ft.Dropdown(
            label="Ch",
            width=80,
            dense=True,
            options=[ft.dropdown.Option("All")],
            value="All",
            **{_DD_EVENT: self._on_filter_changed},
        )
        self._dir_dropdown = ft.Dropdown(
            label="方向",
            width=80,
            dense=True,
            options=[
                ft.dropdown.Option("All"),
                ft.dropdown.Option("Rx"),
                ft.dropdown.Option("Tx"),
            ],
            value="All",
            **{_DD_EVENT: self._on_filter_changed},
        )
        self._type_dropdown = ft.Dropdown(
            label="Type",
            width=100,
            dense=True,
            options=[
                ft.dropdown.Option("All"),
                ft.dropdown.Option("CANFD"),
                ft.dropdown.Option("CAN"),
            ],
            value="All",
            **{_DD_EVENT: self._on_filter_changed},
        )
        self._count_text = ft.Text("0 / 0 frames", size=12)

        filter_bar = ft.Row(
            controls=[
                self._search_field,
                self._channel_dropdown,
                self._dir_dropdown,
                self._type_dropdown,
                ft.VerticalDivider(width=1),
                self._count_text,
                ft.IconButton(ft.Icons.FIRST_PAGE, on_click=self._go_first, tooltip="先頭"),
                ft.IconButton(ft.Icons.NAVIGATE_BEFORE, on_click=self._go_prev, tooltip="前ページ"),
                ft.IconButton(ft.Icons.NAVIGATE_NEXT, on_click=self._go_next, tooltip="次ページ"),
                ft.IconButton(ft.Icons.LAST_PAGE, on_click=self._go_last, tooltip="末尾"),
            ],
            spacing=8,
            alignment=ft.MainAxisAlignment.START,
        )

        # テーブル
        self._data_table = ft.DataTable(
            columns=[
                ft.DataColumn(ft.Text("Time", size=12, weight=ft.FontWeight.BOLD)),
                ft.DataColumn(ft.Text("Ch", size=12, weight=ft.FontWeight.BOLD)),
                ft.DataColumn(ft.Text("ID", size=12, weight=ft.FontWeight.BOLD)),
                ft.DataColumn(ft.Text("Name", size=12, weight=ft.FontWeight.BOLD)),
                ft.DataColumn(ft.Text("Dir", size=12, weight=ft.FontWeight.BOLD)),
                ft.DataColumn(ft.Text("Type", size=12, weight=ft.FontWeight.BOLD)),
                ft.DataColumn(ft.Text("DLC", size=12, weight=ft.FontWeight.BOLD), numeric=True),
                ft.DataColumn(ft.Text("Data", size=12, weight=ft.FontWeight.BOLD)),
            ],
            column_spacing=12,
            data_row_min_height=28,
            data_row_max_height=28,
            heading_row_height=32,
            horizontal_lines=ft.BorderSide(0.5, ft.Colors.OUTLINE),
        )

        table_container = ft.Container(
            content=ft.Column(
                controls=[self._data_table],
                scroll=ft.ScrollMode.AUTO,
                expand=True,
            ),
            expand=True,
        )

        self.controls = [
            ft.Container(content=filter_bar, padding=ft.padding.symmetric(horizontal=8, vertical=4)),
            table_container,
        ]

    def set_frames(self, frames: List[CanFrame]) -> None:
        """フレームデータを設定する"""
        self._all_frames = frames
        # チャンネルドロップダウンを更新
        channels = sorted(set(f.channel for f in frames))
        self._channel_dropdown.options = [ft.dropdown.Option("All")] + [
            ft.dropdown.Option(str(ch)) for ch in channels
        ]
        self._channel_dropdown.value = "All"
        self._page_index = 0
        self._apply_filter()

    def _apply_filter(self) -> None:
        """フィルタを適用してテーブルを更新"""
        search = self._search_field.value.strip().upper() if self._search_field.value else ""
        ch_val = self._channel_dropdown.value
        dir_val = self._dir_dropdown.value
        type_val = self._type_dropdown.value

        filtered = []
        for f in self._all_frames:
            # チャンネルフィルタ
            if ch_val and ch_val != "All" and f.channel != int(ch_val):
                continue
            # 方向フィルタ
            if dir_val and dir_val != "All" and f.dir_str != dir_val:
                continue
            # タイプフィルタ
            if type_val and type_val != "All" and f.type_str != type_val:
                continue
            # テキスト検索
            if search:
                id_hex = f.id_hex.upper()
                name = (f.frame_name or "").upper()
                if search not in id_hex and search not in name:
                    continue
            filtered.append(f)

        self._filtered_frames = filtered
        self._update_table()

    def _update_table(self) -> None:
        """テーブル行を現在のページ分だけ生成する"""
        total = len(self._filtered_frames)
        start = self._page_index * PAGE_SIZE
        end = min(start + PAGE_SIZE, total)
        page_frames = self._filtered_frames[start:end]

        rows = []
        for f in page_frames:
            rows.append(ft.DataRow(
                cells=[
                    ft.DataCell(ft.Text(f"{f.timestamp:.6f}", size=11, font_family="Consolas")),
                    ft.DataCell(ft.Text(str(f.channel), size=11)),
                    ft.DataCell(ft.Text(f.id_hex, size=11, font_family="Consolas")),
                    ft.DataCell(ft.Text(f.frame_name or "", size=11)),
                    ft.DataCell(ft.Text(f.dir_str, size=11)),
                    ft.DataCell(ft.Text(f.type_str, size=11)),
                    ft.DataCell(ft.Text(str(f.data_length), size=11)),
                    ft.DataCell(ft.Text(f.data_hex, size=11, font_family="Consolas")),
                ],
                data=f,
                **{_DR_EVENT: self._on_row_selected},
            ))

        self._data_table.rows = rows
        page_num = self._page_index + 1
        total_pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
        self._count_text.value = f"{start+1}-{end} / {total} frames (p.{page_num}/{total_pages})"

    def _on_filter_changed(self, e=None) -> None:
        self._page_index = 0
        self._apply_filter()
        self.update()

    def _on_row_selected(self, e) -> None:
        if self._on_frame_select and e.control.data:
            self._on_frame_select(e.control.data)

    def _go_first(self, e=None) -> None:
        self._page_index = 0
        self._update_table()
        self.update()

    def _go_prev(self, e=None) -> None:
        if self._page_index > 0:
            self._page_index -= 1
            self._update_table()
            self.update()

    def _go_next(self, e=None) -> None:
        total = len(self._filtered_frames)
        max_page = max((total + PAGE_SIZE - 1) // PAGE_SIZE - 1, 0)
        if self._page_index < max_page:
            self._page_index += 1
            self._update_table()
            self.update()

    def _go_last(self, e=None) -> None:
        total = len(self._filtered_frames)
        self._page_index = max((total + PAGE_SIZE - 1) // PAGE_SIZE - 1, 0)
        self._update_table()
        self.update()

    def get_filtered_frame_ids(self) -> Set[int]:
        """現在のフィルタで表示中のフレーム ID セットを返す"""
        return set(f.arbitration_id for f in self._filtered_frames)
