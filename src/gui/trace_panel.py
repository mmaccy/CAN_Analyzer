"""トレースパネル — CANoe Trace 風フレーム一覧表示

フィルタバー + ページ分割スクロール + 選択行シグナルデコード表示。

設計判断:
- Flet 0.84 の `ft.Dropdown` は選択イベント挙動が不安定なため、`PopupMenuButton`
  を **コンポジション** で包んだ `_FilterSelector` を使う（Flet 0.84 の
  dataclass ベースコントロールは継承が不安定でフィールド初期化が壊れることが
  あるため、継承は避ける）。
- 行の表示は `ft.ListView` を TracePanel 直下に直接配置し、空状態とは
  `visible` トグルで切り替え（Container で包むとスクロール領域の高さ計算が
  伝播しないケースに遭遇したため）。
- `ScrollableControl.scroll_to` は Flet 0.84 で coroutine 化されたため、
  `page.run_task` 経由で実行する。
- 大容量ログ対策としてページング（PAGE_SIZE=2000 行/ページ）を維持。
"""

import bisect
import flet as ft
from typing import Callable, List, Optional, Set

from models.can_frame import CanFrame
from can_parser.dbc_loader import DbcLoader


# 行高
_ROW_HEIGHT = 24
# 1 ページあたりの行数（Flet 0.84 の IPC 転送を抑えるため小さめに）
PAGE_SIZE = 500

# カラム幅定義
_COL_WIDTHS = {
    "time": 110,
    "ch": 36,
    "id": 92,
    "name": 200,
    "dir": 36,
    "type": 56,
    "dlc": 36,
    "data": 520,
}


def _text_cell(value: str, width: int, mono: bool = False) -> ft.Container:
    """固定幅セル"""
    return ft.Container(
        content=ft.Text(
            value,
            size=11,
            font_family="Consolas" if mono else None,
            no_wrap=True,
            overflow=ft.TextOverflow.ELLIPSIS,
        ),
        width=width,
        padding=ft.padding.symmetric(horizontal=4),
    )


class _FilterSelector:
    """Dropdown 代替の単一選択ウィジェット (コンポジション版)

    `.control` プロパティで実体の `ft.PopupMenuButton` を返す。
    選択時に `on_change()` コールバックを発火。値は `value` プロパティで取得。
    """

    def __init__(
        self,
        label: str,
        options: List[str],
        value: str,
        on_change: Callable[[], None],
        width: int = 100,
    ) -> None:
        self._label = label
        self._options = list(options)
        self._value = value
        self._on_change = on_change
        self._width = width

        self._display_text = ft.Text(self._format_display(), size=12, no_wrap=True)
        self._button = ft.PopupMenuButton(
            content=ft.Container(
                content=ft.Row(
                    controls=[
                        self._display_text,
                        ft.Icon(ft.Icons.ARROW_DROP_DOWN, size=18),
                    ],
                    spacing=2,
                    tight=True,
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                ),
                padding=ft.padding.symmetric(horizontal=8, vertical=6),
                border=ft.border.all(1, ft.Colors.OUTLINE),
                border_radius=4,
                width=self._width,
                bgcolor=ft.Colors.SURFACE_CONTAINER_LOWEST,
            ),
            items=self._build_items(),
        )

    def _format_display(self) -> str:
        return f"{self._label}: {self._value}"

    def _build_items(self) -> List[ft.PopupMenuItem]:
        # Flet 0.84 の PopupMenuItem は `text` を持たず `content` を使う
        return [
            ft.PopupMenuItem(
                content=ft.Text(opt, size=12),
                data=opt,
                on_click=self._on_item_click,
            )
            for opt in self._options
        ]

    def _on_item_click(self, e) -> None:
        new_val = e.control.data
        if new_val == self._value:
            return
        self._value = new_val
        self._display_text.value = self._format_display()
        try:
            self._display_text.update()
        except (AssertionError, RuntimeError):
            # page 未アタッチ時は update を飛ばす（初期化中など）
            pass
        self._on_change()

    @property
    def control(self) -> ft.PopupMenuButton:
        return self._button

    @property
    def value(self) -> str:
        return self._value

    def set_options(self, options: List[str], value: Optional[str] = None) -> None:
        self._options = list(options)
        if value is not None and value in options:
            self._value = value
        elif self._value not in options and options:
            self._value = options[0]
        self._display_text.value = self._format_display()
        self._button.items = self._build_items()


class TracePanel(ft.Column):
    """トレースビュー: ページ分割フレーム一覧 + シグナルデコード"""

    def __init__(self, on_frame_select: Optional[Callable] = None):
        super().__init__(expand=True, spacing=0)
        self._all_frames: List[CanFrame] = []
        self._filtered_frames: List[CanFrame] = []
        self._dbc_loader: Optional[DbcLoader] = None
        self._on_frame_select = on_frame_select
        self._page_index: int = 0
        self._selected_global_index: int = -1

        # フィルタ UI
        self._search_field = ft.TextField(
            label="検索 (ID / フレーム名)",
            width=250,
            dense=True,
            on_submit=self._on_filter_changed,
            on_change=self._on_filter_changed,
        )
        self._channel_selector = _FilterSelector(
            label="Ch", options=["All"], value="All",
            on_change=self._on_filter_changed, width=100,
        )
        self._dir_selector = _FilterSelector(
            label="方向", options=["All", "Rx", "Tx"], value="All",
            on_change=self._on_filter_changed, width=110,
        )
        self._type_selector = _FilterSelector(
            label="Type", options=["All", "CANFD", "CAN"], value="All",
            on_change=self._on_filter_changed, width=120,
        )
        self._jump_field = ft.TextField(
            label="Jump (時刻 s)",
            width=130,
            dense=True,
            on_submit=self._on_jump,
        )
        self._count_text = ft.Text("0 frames", size=12)

        # ページナビゲーション
        self._first_btn = ft.IconButton(
            ft.Icons.FIRST_PAGE, on_click=self._go_first, tooltip="先頭ページ"
        )
        self._prev_btn = ft.IconButton(
            ft.Icons.NAVIGATE_BEFORE, on_click=self._go_prev, tooltip="前ページ"
        )
        self._next_btn = ft.IconButton(
            ft.Icons.NAVIGATE_NEXT, on_click=self._go_next, tooltip="次ページ"
        )
        self._last_btn = ft.IconButton(
            ft.Icons.LAST_PAGE, on_click=self._go_last, tooltip="末尾ページ"
        )

        filter_bar = ft.Row(
            controls=[
                self._search_field,
                self._channel_selector.control,
                self._dir_selector.control,
                self._type_selector.control,
                self._jump_field,
                ft.VerticalDivider(width=1),
                self._count_text,
                self._first_btn,
                self._prev_btn,
                self._next_btn,
                self._last_btn,
            ],
            spacing=8,
            alignment=ft.MainAxisAlignment.START,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        # ヘッダ行
        header_row = ft.Container(
            content=ft.Row(
                controls=[
                    _text_cell("Time", _COL_WIDTHS["time"]),
                    _text_cell("Ch", _COL_WIDTHS["ch"]),
                    _text_cell("ID", _COL_WIDTHS["id"]),
                    _text_cell("Name", _COL_WIDTHS["name"]),
                    _text_cell("Dir", _COL_WIDTHS["dir"]),
                    _text_cell("Type", _COL_WIDTHS["type"]),
                    _text_cell("DLC", _COL_WIDTHS["dlc"]),
                    _text_cell("Data", _COL_WIDTHS["data"]),
                ],
                spacing=0,
                tight=True,
            ),
            height=26,
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH,
            padding=ft.padding.symmetric(horizontal=4, vertical=2),
            border=ft.border.only(bottom=ft.BorderSide(1, ft.Colors.OUTLINE)),
        )
        for cell in header_row.content.controls:
            cell.content.weight = ft.FontWeight.BOLD

        # 空状態プレースホルダとスクロール可能な行リストは同時に親 Column に置かず、
        # Container(expand=True) の content 差し替えで切替える。
        # 両方 expand=True で並存させると Flet 0.84 の flex 割当が不安定になるため。
        self._empty_placeholder = ft.Container(
            content=ft.Text("ASC ファイルを読み込んでください", italic=True, size=12),
            alignment=ft.Alignment(0, 0),
            expand=True,
        )
        self._list_view = ft.ListView(
            expand=True,
            item_extent=_ROW_HEIGHT,
            spacing=0,
        )
        self._body = ft.Container(
            content=self._empty_placeholder,
            expand=True,
        )

        # シグナルデコード表示パネル
        self._detail_column = ft.Column(
            controls=[
                ft.Text("行をクリックするとシグナル値を表示します", italic=True, size=11),
            ],
            scroll=ft.ScrollMode.AUTO,
            spacing=0,
            tight=True,
        )
        self._detail_container = ft.Container(
            content=self._detail_column,
            height=180,
            bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
            padding=ft.padding.symmetric(horizontal=8, vertical=4),
            border=ft.border.only(top=ft.BorderSide(1, ft.Colors.OUTLINE)),
        )

        # TracePanel 自身が Column。flex 子として以下を配置:
        # - filter_bar (固定高)
        # - header_row (高さ26)
        # - _body (expand=True): content=_empty_placeholder or _list_view
        # - _detail_container (height=180)
        self.controls = [
            ft.Container(content=filter_bar, padding=ft.padding.symmetric(horizontal=8, vertical=4)),
            header_row,
            self._body,
            self._detail_container,
        ]

    # ---------- Public API ----------

    def set_frames(self, frames: List[CanFrame]) -> None:
        """フレームデータを設定する"""
        self._all_frames = frames
        channels = sorted(set(f.channel for f in frames))
        ch_options = ["All"] + [str(ch) for ch in channels]
        self._channel_selector.set_options(ch_options, value="All")
        self._selected_global_index = -1
        self._page_index = 0
        self._apply_filter()

    def set_dbc(self, dbc_loader: Optional[DbcLoader]) -> None:
        """DBC を設定する（選択行のデコード表示に使用）"""
        self._dbc_loader = dbc_loader
        if 0 <= self._selected_global_index < len(self._filtered_frames):
            self._update_detail(self._filtered_frames[self._selected_global_index])

    def get_filtered_frame_ids(self) -> Set[int]:
        """現在のフィルタで表示中のフレーム ID セットを返す"""
        return set(f.arbitration_id for f in self._filtered_frames)

    # ---------- Filtering / paging ----------

    def _apply_filter(self) -> None:
        search = self._search_field.value.strip().upper() if self._search_field.value else ""
        ch_val = self._channel_selector.value
        dir_val = self._dir_selector.value
        type_val = self._type_selector.value

        filtered = []
        for f in self._all_frames:
            if ch_val != "All" and f.channel != int(ch_val):
                continue
            if dir_val != "All" and f.dir_str != dir_val:
                continue
            if type_val != "All" and f.type_str != type_val:
                continue
            if search:
                id_hex = f.id_hex.upper()
                name = (f.frame_name or "").upper()
                if search not in id_hex and search not in name:
                    continue
            filtered.append(f)

        self._filtered_frames = filtered
        self._selected_global_index = -1
        self._page_index = 0
        self._render_current_page()
        self._reset_detail()

    def _render_current_page(self) -> None:
        """現在のページ範囲の行だけを構築して ListView に投入する"""
        total = len(self._filtered_frames)
        if total == 0:
            self._count_text.value = "0 frames"
            self._list_view.controls = []
            self._body.content = self._empty_placeholder
            return

        total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
        if self._page_index < 0:
            self._page_index = 0
        if self._page_index >= total_pages:
            self._page_index = total_pages - 1

        start = self._page_index * PAGE_SIZE
        end = min(start + PAGE_SIZE, total)

        rows: List[ft.Control] = [
            self._build_row(i, self._filtered_frames[i]) for i in range(start, end)
        ]
        self._list_view.controls = rows
        self._body.content = self._list_view

        self._count_text.value = (
            f"{start+1:,}-{end:,} / {total:,} frames  (p.{self._page_index+1}/{total_pages})"
        )

    def _build_row(self, global_index: int, f: CanFrame) -> ft.Control:
        """単一行 Container を生成する"""
        is_selected = global_index == self._selected_global_index
        return ft.Container(
            content=ft.Row(
                controls=[
                    _text_cell(f"{f.timestamp:.6f}", _COL_WIDTHS["time"], mono=True),
                    _text_cell(str(f.channel), _COL_WIDTHS["ch"]),
                    _text_cell(f.id_hex, _COL_WIDTHS["id"], mono=True),
                    _text_cell(f.frame_name or "", _COL_WIDTHS["name"]),
                    _text_cell(f.dir_str, _COL_WIDTHS["dir"]),
                    _text_cell(f.type_str, _COL_WIDTHS["type"]),
                    _text_cell(str(f.data_length), _COL_WIDTHS["dlc"]),
                    _text_cell(f.data_hex, _COL_WIDTHS["data"], mono=True),
                ],
                spacing=0,
                tight=True,
            ),
            height=_ROW_HEIGHT,
            padding=ft.padding.symmetric(horizontal=4, vertical=0),
            bgcolor=ft.Colors.PRIMARY_CONTAINER if is_selected else None,
            data=global_index,
            on_click=self._on_row_click,
        )

    # ---------- Row selection / detail ----------

    def _on_row_click(self, e) -> None:
        new_global = e.control.data
        if new_global == self._selected_global_index:
            return

        old_global = self._selected_global_index
        self._selected_global_index = new_global

        page_start = self._page_index * PAGE_SIZE
        rows = self._list_view.controls
        if 0 <= old_global - page_start < len(rows):
            rows[old_global - page_start].bgcolor = None
            rows[old_global - page_start].update()
        if 0 <= new_global - page_start < len(rows):
            rows[new_global - page_start].bgcolor = ft.Colors.PRIMARY_CONTAINER
            rows[new_global - page_start].update()

        frame = self._filtered_frames[new_global]
        self._update_detail(frame)
        if self._on_frame_select:
            self._on_frame_select(frame)

    def _reset_detail(self) -> None:
        self._detail_column.controls = [
            ft.Text("行をクリックするとシグナル値を表示します", italic=True, size=11),
        ]

    def _update_detail(self, frame: CanFrame) -> None:
        """選択行のシグナルデコード結果を下部パネルに表示"""
        header = ft.Row(
            controls=[
                ft.Text(
                    f"{frame.id_hex}  {frame.frame_name or '(未定義)'}",
                    size=12,
                    weight=ft.FontWeight.BOLD,
                    font_family="Consolas",
                ),
                ft.Text(
                    f"t={frame.timestamp:.6f}s  Ch={frame.channel}  DLC={frame.data_length}  {frame.type_str}",
                    size=11,
                    color=ft.Colors.ON_SURFACE_VARIANT,
                ),
            ],
            spacing=16,
        )

        controls: List[ft.Control] = [header]

        if self._dbc_loader is None or not self._dbc_loader.loaded_files:
            controls.append(
                ft.Text("DB 未読込: 'DB 開く' からデータベースを読み込んでください", italic=True, size=11)
            )
            self._detail_column.controls = controls
            return

        signals = self._dbc_loader.decode_frame(frame)
        if not signals:
            controls.append(ft.Text("DB にこのフレームの定義がありません", italic=True, size=11))
            self._detail_column.controls = controls
            return

        sig_header = ft.Row(
            controls=[
                _text_cell("Signal", 220),
                _text_cell("Raw", 90),
                _text_cell("Physical", 120),
                _text_cell("Unit", 80),
            ],
            spacing=0,
            tight=True,
        )
        for c in sig_header.controls:
            c.content.weight = ft.FontWeight.W_500
            c.content.size = 11
        controls.append(
            ft.Container(
                content=sig_header,
                padding=ft.padding.symmetric(horizontal=2, vertical=2),
                border=ft.border.only(bottom=ft.BorderSide(0.5, ft.Colors.OUTLINE_VARIANT)),
            )
        )

        for sv in signals:
            controls.append(
                ft.Row(
                    controls=[
                        _text_cell(sv.signal_name, 220, mono=True),
                        _text_cell(str(sv.raw_value), 90, mono=True),
                        _text_cell(f"{sv.physical_value:.6g}", 120, mono=True),
                        _text_cell(sv.unit, 80),
                    ],
                    spacing=0,
                    tight=True,
                )
            )
        self._detail_column.controls = controls

    # ---------- Navigation ----------

    def _schedule_scroll(self, offset_px: float) -> None:
        """ListView の scroll_to は coroutine のため page.run_task で実行する"""
        page = getattr(self, "page", None)
        if page is None:
            return
        list_view = self._list_view

        async def _do_scroll():
            try:
                await list_view.scroll_to(offset=offset_px, duration=200)
            except Exception:
                pass

        try:
            page.run_task(_do_scroll)
        except (AttributeError, RuntimeError):
            pass

    def _go_first(self, e=None) -> None:
        if self._page_index != 0:
            self._page_index = 0
            self._render_current_page()
            self.update()

    def _go_prev(self, e=None) -> None:
        if self._page_index > 0:
            self._page_index -= 1
            self._render_current_page()
            self.update()

    def _go_next(self, e=None) -> None:
        total = len(self._filtered_frames)
        max_page = max((total + PAGE_SIZE - 1) // PAGE_SIZE - 1, 0)
        if self._page_index < max_page:
            self._page_index += 1
            self._render_current_page()
            self.update()

    def _go_last(self, e=None) -> None:
        total = len(self._filtered_frames)
        max_page = max((total + PAGE_SIZE - 1) // PAGE_SIZE - 1, 0)
        if self._page_index != max_page:
            self._page_index = max_page
            self._render_current_page()
            self.update()

    def _on_jump(self, e=None) -> None:
        """指定時刻に最も近い行を含むページへ遷移し、ページ内でスクロール"""
        text = (self._jump_field.value or "").strip()
        if not text or not self._filtered_frames:
            return
        try:
            target = float(text)
        except ValueError:
            return
        times = [f.timestamp for f in self._filtered_frames]
        idx = bisect.bisect_left(times, target)
        if idx >= len(times):
            idx = len(times) - 1

        new_page = idx // PAGE_SIZE
        if new_page != self._page_index:
            self._page_index = new_page
            self._render_current_page()
            self.update()

        offset_in_page = idx - self._page_index * PAGE_SIZE
        self._schedule_scroll(offset_in_page * _ROW_HEIGHT)

    def _on_filter_changed(self, e=None) -> None:
        self._apply_filter()
        self.update()
