"""シグナルツリーパネル — DBC フレーム/シグナル階層表示

DBC に基づくフレーム・シグナルのツリー表示。
チェックボックスでグラフ表示対象シグナルを選択。
"""

import flet as ft
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from can_parser.dbc_loader import DbcLoader


# ツールチップで 1 シグナルあたり何件までの choice（Value Table）を表示するか。
# これを超える場合は末尾を省略表記に切り替える。
_MAX_TOOLTIP_CHOICES = 20


def _build_signal_tooltip(signal: Any, frame_name: str, frame_id: int) -> str:
    """cantools Signal オブジェクトから表示用の詳細ツールチップを組み立てる。

    ARXML / DBC から読み取れる情報を漏れなく拾うが、未設定属性は行ごと省く。
    """
    lines: List[str] = []

    # 基本: フレーム情報 + シグナル名
    lines.append(f"{signal.name}")
    lines.append(f"Frame: {frame_name} (0x{frame_id:X})")

    # 説明 (Description / Comment)
    comment = getattr(signal, "comment", None)
    if isinstance(comment, dict):
        # 多言語 dict の場合はデフォルト or 最初の値
        comment = comment.get(None) or next(iter(comment.values()), None)
    if comment:
        lines.append("")
        lines.append(f"説明: {comment}")

    # ビット配置・型
    bit_start = getattr(signal, "start", None)
    bit_length = getattr(signal, "length", None)
    byte_order = getattr(signal, "byte_order", None)
    is_signed = getattr(signal, "is_signed", None)
    is_float = getattr(signal, "is_float", None)
    lines.append("")
    if bit_start is not None and bit_length is not None:
        lines.append(f"ビット位置: bit{bit_start}, 長さ: {bit_length}")
    if byte_order:
        lines.append(f"バイトオーダー: {byte_order}")
    type_parts = []
    if is_float:
        type_parts.append("float")
    elif is_signed is not None:
        type_parts.append("signed" if is_signed else "unsigned")
    if type_parts:
        lines.append(f"型: {' / '.join(type_parts)}")

    # 物理値変換 (factor / offset / unit / min / max)
    scale = getattr(signal, "scale", None)
    offset = getattr(signal, "offset", None)
    unit = getattr(signal, "unit", None)
    minimum = getattr(signal, "minimum", None)
    maximum = getattr(signal, "maximum", None)
    initial = getattr(signal, "initial", None)
    conv_lines = []
    if scale is not None:
        conv_lines.append(f"factor: {scale}")
    if offset is not None:
        conv_lines.append(f"offset: {offset}")
    if unit:
        conv_lines.append(f"unit: {unit}")
    if conv_lines:
        lines.append("")
        lines.append("物理値: " + ", ".join(conv_lines))
    range_parts = []
    if minimum is not None:
        range_parts.append(f"min={minimum}")
    if maximum is not None:
        range_parts.append(f"max={maximum}")
    if initial is not None:
        range_parts.append(f"initial={initial}")
    if range_parts:
        lines.append("範囲: " + ", ".join(range_parts))

    # Value Table (choices)
    choices = getattr(signal, "choices", None)
    if choices:
        lines.append("")
        lines.append("値定義:")
        items = list(choices.items())
        for raw_val, label in items[:_MAX_TOOLTIP_CHOICES]:
            lines.append(f"  {raw_val} = {label}")
        if len(items) > _MAX_TOOLTIP_CHOICES:
            lines.append(f"  ... (他 {len(items) - _MAX_TOOLTIP_CHOICES} 項目)")

    # 送受信ノード（任意情報）
    receivers = getattr(signal, "receivers", None)
    if receivers:
        lines.append("")
        lines.append("受信ノード: " + ", ".join(str(r) for r in receivers))

    # 多重化情報
    if getattr(signal, "is_multiplexer", False):
        lines.append("")
        lines.append("(マルチプレクサシグナル)")
    mux_ids = getattr(signal, "multiplexer_ids", None)
    mux_signal = getattr(signal, "multiplexer_signal", None)
    if mux_signal:
        lines.append(f"多重化親: {mux_signal} (id={list(mux_ids) if mux_ids else '?'})")

    return "\n".join(lines)


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
                    tooltip=_build_signal_tooltip(sig, frame_name, frame_id),
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
