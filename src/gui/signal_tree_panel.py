"""シグナルツリーパネル — DBC フレーム/シグナル階層表示

DBC に基づくフレーム・シグナルのツリー表示。
チェックボックスでグラフ表示対象シグナルを選択。
"""

import flet as ft
from typing import Callable, Dict, List, Optional, Set, Tuple

from can_parser.dbc_loader import DbcLoader


class SignalTreePanel(ft.Column):
    """DBC シグナルツリー"""

    def __init__(self, on_selection_changed: Optional[Callable] = None):
        super().__init__(expand=True, spacing=0)
        self._dbc_loader: Optional[DbcLoader] = None
        self._on_selection_changed = on_selection_changed
        # {(frame_id, signal_name): True/False}
        self._selected_signals: Dict[Tuple[int, str], bool] = {}
        self._log_frame_ids: Set[int] = set()
        self._current_search: str = ""

        self._search_field = ft.TextField(
            label="シグナル検索",
            dense=True,
            expand=True,
            on_change=self._on_search_changed,
        )

        # 選択中シグナル表示セクション（折りたたみ時も常時表示）
        self._selected_column = ft.Column(spacing=0, tight=True)
        self._selected_container = ft.Container(
            content=self._selected_column,
            padding=ft.padding.symmetric(horizontal=4, vertical=2),
            visible=False,
        )

        self._tree_column = ft.Column(
            scroll=ft.ScrollMode.AUTO,
            expand=True,
            spacing=0,
        )

        self.controls = [
            ft.Container(
                content=self._search_field,
                padding=ft.padding.symmetric(horizontal=4, vertical=4),
            ),
            self._selected_container,
            ft.Container(
                content=self._tree_column,
                expand=True,
            ),
        ]

    def set_dbc(self, dbc_loader: DbcLoader) -> None:
        """DBC データを設定してツリーを構築する"""
        self._dbc_loader = dbc_loader
        self._selected_signals.clear()
        self._build_tree()
        self._rebuild_selected_section()

    def set_log_frame_ids(self, ids: Set[int]) -> None:
        """ログに含まれるフレーム ID をハイライト用に設定"""
        self._log_frame_ids = ids
        self._build_tree()

    def get_selected_signals(self) -> List[Tuple[int, str]]:
        """選択中のシグナルリスト [(frame_id, signal_name), ...]"""
        return [k for k, v in self._selected_signals.items() if v]

    def set_selected_signals(self, signals: List[Tuple[int, str]]) -> None:
        """選択状態を一括設定する（設定ファイル読込用）"""
        self._selected_signals = {tuple(sig): True for sig in signals}
        self._build_tree(self._current_search)
        self._rebuild_selected_section()
        if self._on_selection_changed:
            self._on_selection_changed(self.get_selected_signals())

    def _build_tree(self, search: str = "") -> None:
        """ツリーコントロールを構築する"""
        self._current_search = search
        if not self._dbc_loader:
            self._tree_column.controls = [ft.Text("DBC ファイルを読み込んでください", italic=True, size=12)]
            return

        search_upper = search.upper()
        items = []

        for msg in sorted(self._dbc_loader.messages, key=lambda m: m.frame_id):
            frame_id = msg.frame_id
            frame_name = msg.name
            in_log = frame_id in self._log_frame_ids

            # 検索フィルタ
            if search_upper:
                frame_match = search_upper in frame_name.upper() or search_upper in f"{frame_id:X}".upper()
                signal_match = any(search_upper in s.name.upper() for s in msg.signals)
                if not frame_match and not signal_match:
                    continue

            # フレーム名ラベル
            id_hex = f"0x{frame_id:X}"
            badge_color = ft.Colors.GREEN if in_log else ft.Colors.GREY
            frame_label = ft.Row(
                controls=[
                    ft.Icon(ft.Icons.CIRCLE, size=8, color=badge_color),
                    ft.Text(f"{frame_name} ({id_hex})", size=12, weight=ft.FontWeight.W_500),
                ],
                spacing=4,
            )

            # シグナルチェックボックス行
            signal_controls = []
            for sig in msg.signals:
                if search_upper and search_upper not in sig.name.upper() and not (search_upper in frame_name.upper()):
                    continue

                key = (frame_id, sig.name)
                checked = self._selected_signals.get(key, False)
                unit_str = f" [{sig.unit}]" if sig.unit else ""
                detail = f"  bit{sig.start}:{sig.length}{unit_str}"

                cb = ft.Checkbox(
                    label=f"{sig.name}{detail}",
                    value=checked,
                    data=key,
                    on_change=self._on_signal_check_changed,
                    label_style=ft.TextStyle(size=11, font_family="Consolas"),
                    height=28,
                )
                signal_controls.append(cb)

            _expand_kwarg = "expanded" if "expanded" in ft.ExpansionTile.__init__.__code__.co_varnames else "initially_expanded"
            exp_tile = ft.ExpansionTile(
                title=frame_label,
                controls=signal_controls,
                **{_expand_kwarg: bool(search_upper)},
                tile_padding=ft.padding.symmetric(horizontal=8, vertical=0),
                controls_padding=ft.padding.only(left=24),
            )
            items.append(exp_tile)

        if not items:
            items = [ft.Text("該当なし", italic=True, size=12)]

        self._tree_column.controls = items

    def _rebuild_selected_section(self) -> None:
        """選択中シグナルセクションを再構築する（折りたたみ時も常時表示）"""
        selected = self.get_selected_signals()
        if not selected:
            self._selected_column.controls = []
            self._selected_container.visible = False
            return

        # 所属フレーム名も併記（frame_id から逆引き、失敗時は hex 表示）
        frame_name_map: Dict[int, str] = {}
        if self._dbc_loader:
            for msg in self._dbc_loader.messages:
                frame_name_map[msg.frame_id] = msg.name

        rows: List[ft.Control] = [
            ft.Container(
                content=ft.Row(
                    controls=[
                        ft.Icon(ft.Icons.CHECK_CIRCLE, size=12, color=ft.Colors.PRIMARY),
                        ft.Text(
                            f"選択中 ({len(selected)})",
                            size=11,
                            weight=ft.FontWeight.W_500,
                            color=ft.Colors.PRIMARY,
                        ),
                        ft.Container(expand=True),
                        ft.TextButton(
                            "全解除",
                            on_click=self._on_clear_all_selections,
                            style=ft.ButtonStyle(padding=ft.padding.symmetric(horizontal=6, vertical=0)),
                        ),
                    ],
                    spacing=4,
                    tight=True,
                ),
                padding=ft.padding.symmetric(horizontal=4, vertical=2),
            ),
        ]
        for fid, sname in selected:
            fname = frame_name_map.get(fid, f"0x{fid:X}")
            rows.append(
                ft.Container(
                    content=ft.Row(
                        controls=[
                            ft.IconButton(
                                ft.Icons.CLOSE,
                                icon_size=14,
                                tooltip="選択解除",
                                data=(fid, sname),
                                on_click=self._on_remove_selected,
                                style=ft.ButtonStyle(padding=ft.padding.all(2)),
                            ),
                            ft.Text(
                                sname,
                                size=11,
                                font_family="Consolas",
                                weight=ft.FontWeight.W_500,
                            ),
                            ft.Text(
                                f"({fname})",
                                size=10,
                                color=ft.Colors.ON_SURFACE_VARIANT,
                            ),
                        ],
                        spacing=4,
                        tight=True,
                    ),
                    padding=ft.padding.only(left=4, right=4),
                )
            )
        self._selected_column.controls = rows
        self._selected_container.visible = True

    def _on_signal_check_changed(self, e) -> None:
        key = e.control.data
        self._selected_signals[key] = e.control.value
        self._rebuild_selected_section()
        if self._on_selection_changed:
            self._on_selection_changed(self.get_selected_signals())
        self.update()

    def _on_remove_selected(self, e) -> None:
        """選択中セクションの × ボタンで個別解除"""
        key = e.control.data
        self._selected_signals[key] = False
        # ツリー側のチェックボックス状態を同期するため再構築
        self._build_tree(self._current_search)
        self._rebuild_selected_section()
        if self._on_selection_changed:
            self._on_selection_changed(self.get_selected_signals())
        self.update()

    def _on_clear_all_selections(self, e) -> None:
        """全選択解除"""
        if not any(self._selected_signals.values()):
            return
        self._selected_signals.clear()
        self._build_tree(self._current_search)
        self._rebuild_selected_section()
        if self._on_selection_changed:
            self._on_selection_changed(self.get_selected_signals())
        self.update()

    def _on_search_changed(self, e=None) -> None:
        search = self._search_field.value.strip() if self._search_field.value else ""
        self._build_tree(search)
        self.update()
