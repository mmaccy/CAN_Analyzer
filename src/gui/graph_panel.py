"""グラフパネル — Plotly ベースの時系列グラフ表示

選択シグナルの値を時系列グラフでインタラクティブに表示する。
"""

import flet as ft
try:
    from flet.plotly_chart import PlotlyChart
except ModuleNotFoundError:
    from flet_charts.plotly_chart import PlotlyChart
from typing import Dict, List, Optional, Set, Tuple

from models.can_frame import CanFrame
from models.signal_value import SignalValue
from can_parser.dbc_loader import DbcLoader
from analysis.graph_builder import build_overlay_graph, build_subplot_graph


# Plotly の既定カラーシーケンス（凡例 UI のマーカー色と合わせる）
_PLOTLY_COLORS = [
    "#636EFA", "#EF553B", "#00CC96", "#AB63FA", "#FFA15A",
    "#19D3F3", "#FF6692", "#B6E880", "#FF97FF", "#FECB52",
]


class GraphPanel(ft.Column):
    """時系列グラフパネル"""

    def __init__(self):
        super().__init__(expand=True, spacing=0)
        self._frames: List[CanFrame] = []
        self._dbc_loader: Optional[DbcLoader] = None
        self._selected_signals: List[Tuple[int, str]] = []
        self._use_physical = True
        self._use_subplot = False
        # 強調表示中のシグナル名集合。空なら全シグナルを通常描画
        self._highlighted: Set[str] = set()

        # ツールバー
        self._physical_toggle = ft.Switch(
            label="物理値",
            value=True,
            on_change=self._on_toggle_physical,
        )
        self._subplot_toggle = ft.Switch(
            label="サブプロット",
            value=False,
            on_change=self._on_toggle_subplot,
        )
        self._save_png_btn = ft.IconButton(
            ft.Icons.IMAGE, tooltip="PNG 保存", on_click=self._on_save_png
        )
        self._reset_highlight_btn = ft.TextButton(
            "強調解除",
            icon=ft.Icons.CLEAR,
            on_click=self._on_reset_highlight,
            visible=False,
        )
        toolbar = ft.Row(
            controls=[
                self._physical_toggle,
                self._subplot_toggle,
                ft.VerticalDivider(width=1),
                self._save_png_btn,
                self._reset_highlight_btn,
            ],
            spacing=8,
            alignment=ft.MainAxisAlignment.START,
        )

        # 凡例 UI（クリックでシグナルを強調表示）
        self._legend_row = ft.Row(
            controls=[],
            spacing=6,
            wrap=True,
        )
        self._legend_container = ft.Container(
            content=self._legend_row,
            padding=ft.padding.symmetric(horizontal=8, vertical=4),
            visible=False,
        )

        self._chart: Optional[PlotlyChart] = None
        self._placeholder = ft.Container(
            content=ft.Text(
                "シグナルツリーからシグナルを選択してください",
                size=14,
                italic=True,
                text_align=ft.TextAlign.CENTER,
            ),
            expand=True,
            alignment=ft.Alignment(0, 0),
        )
        self._chart_container = ft.Container(
            content=self._placeholder.content,
            expand=True,
            alignment=ft.Alignment(0, 0),
        )

        self.controls = [
            ft.Container(content=toolbar, padding=ft.padding.symmetric(horizontal=8, vertical=4)),
            self._legend_container,
            self._chart_container,
        ]

    def set_data(self, frames: List[CanFrame], dbc_loader: Optional[DbcLoader]) -> None:
        """フレームデータと DBC を設定する"""
        self._frames = frames
        self._dbc_loader = dbc_loader
        if self._selected_signals:
            self._rebuild_chart()

    def update_signals(self, selected_signals: List[Tuple[int, str]]) -> None:
        """選択シグナルが変更された時にグラフを再描画する"""
        self._selected_signals = selected_signals
        # 選択解除されたシグナルの強調状態はクリア
        current_names = {sname for _, sname in selected_signals}
        self._highlighted &= current_names
        self._rebuild_chart()
        self.update()

    def _cycle_time_lookup(self, signal_name: str) -> Optional[float]:
        """シグナル名から DBC/ARXML 定義の送信周期(ms)を返す"""
        if self._dbc_loader is None:
            return None
        # 同名シグナルが複数フレームに存在することを想定し、該当する選択シグナルの
        # frame_id を逆引きする（同名複数選択時は最初に見つかった frame_id の周期を採用）。
        for fid, sname in self._selected_signals:
            if sname == signal_name:
                return self._dbc_loader.get_cycle_time_ms(fid)
        return None

    def _rebuild_chart(self) -> None:
        """グラフを再構築する"""
        if not self._selected_signals or not self._dbc_loader or not self._frames:
            self._chart = None
            self._chart_container.content = self._placeholder.content
            self._legend_container.visible = False
            self._reset_highlight_btn.visible = False
            return

        # シグナルデータを収集
        signal_data = self._collect_signal_data()
        if not signal_data:
            self._chart = None
            self._chart_container.content = self._placeholder.content
            self._legend_container.visible = False
            self._reset_highlight_btn.visible = False
            return

        # グラフ生成
        if self._use_subplot:
            fig = build_subplot_graph(
                signal_data,
                use_physical=self._use_physical,
                cycle_time_lookup=self._cycle_time_lookup,
                highlighted=self._highlighted or None,
            )
        else:
            fig = build_overlay_graph(
                signal_data,
                use_physical=self._use_physical,
                cycle_time_lookup=self._cycle_time_lookup,
                highlighted=self._highlighted or None,
            )

        # 毎回 PlotlyChart を新規生成して Container.content を差し替える。
        # flet 0.84 の object_patch による差分更新では、Column.controls のスロット入替
        # が PlotlyChart の SVG 再生成を正しくトリガしないケースがあるため、
        # Container の content 経由で確実に再レンダリングさせる。
        self._chart = PlotlyChart(figure=fig, expand=True)
        self._chart_container.content = self._chart

        # 凡例 UI を更新
        self._rebuild_legend(list(signal_data.keys()))

    def _rebuild_legend(self, signal_names: List[str]) -> None:
        """凡例チップ列を再構築する"""
        items: List[ft.Control] = []
        for i, name in enumerate(signal_names):
            color = _PLOTLY_COLORS[i % len(_PLOTLY_COLORS)]
            is_highlighted = name in self._highlighted
            # 強調状態を視覚化: 選択時は背景を薄く塗る
            bg = ft.Colors.with_opacity(0.15, color) if is_highlighted else ft.Colors.TRANSPARENT
            border = ft.border.all(
                1.5 if is_highlighted else 1,
                color if is_highlighted else ft.Colors.OUTLINE_VARIANT,
            )
            item = ft.Container(
                content=ft.Row(
                    controls=[
                        ft.Container(
                            width=10,
                            height=10,
                            bgcolor=color,
                            border_radius=5,
                        ),
                        ft.Text(name, size=11, selectable=False),
                    ],
                    spacing=6,
                    tight=True,
                ),
                padding=ft.padding.symmetric(horizontal=8, vertical=3),
                bgcolor=bg,
                border=border,
                border_radius=12,
                data=name,
                on_click=self._on_legend_click,
                tooltip="クリックで強調表示切替",
            )
            items.append(item)
        self._legend_row.controls = items
        self._legend_container.visible = bool(items)
        self._reset_highlight_btn.visible = bool(self._highlighted)

    def _on_legend_click(self, e) -> None:
        """凡例チップクリック: 強調対象にトグル追加/削除"""
        name = e.control.data
        if name in self._highlighted:
            self._highlighted.discard(name)
        else:
            self._highlighted.add(name)
        self._rebuild_chart()
        self.update()

    def _on_reset_highlight(self, e) -> None:
        """強調表示をリセット"""
        if not self._highlighted:
            return
        self._highlighted.clear()
        self._rebuild_chart()
        self.update()

    def _collect_signal_data(self) -> Dict[str, List[SignalValue]]:
        """選択シグナルに対応するデータを収集"""
        if not self._dbc_loader:
            return {}

        # frame_id → [signal_name, ...] のマップ
        target_map: Dict[int, List[str]] = {}
        for fid, sname in self._selected_signals:
            target_map.setdefault(fid, []).append(sname)

        result: Dict[str, List[SignalValue]] = {
            sname: [] for _, sname in self._selected_signals
        }

        for frame in self._frames:
            if frame.arbitration_id not in target_map:
                continue
            target_signals = target_map[frame.arbitration_id]
            decoded = self._dbc_loader.decode_frame(frame)
            for sv in decoded:
                if sv.signal_name in target_signals:
                    result[sv.signal_name].append(sv)

        return {k: v for k, v in result.items() if v}

    def _on_toggle_physical(self, e) -> None:
        self._use_physical = e.control.value
        self._rebuild_chart()
        self.update()

    def _on_toggle_subplot(self, e) -> None:
        self._use_subplot = e.control.value
        self._rebuild_chart()
        self.update()

    def _on_save_png(self, e) -> None:
        # Plotly の built-in ダウンロードを使用
        # (ft.PlotlyChart の config で対応)
        pass
