"""グラフパネル — Plotly ベースの時系列グラフ表示

選択シグナルの値を時系列グラフでインタラクティブに表示する。
"""

import flet as ft
try:
    from flet.plotly_chart import PlotlyChart
except ModuleNotFoundError:
    from flet_charts.plotly_chart import PlotlyChart
from typing import Dict, List, Optional, Tuple

from models.can_frame import CanFrame
from models.signal_value import SignalValue
from can_parser.dbc_loader import DbcLoader
from analysis.graph_builder import build_overlay_graph, build_subplot_graph


class GraphPanel(ft.Column):
    """時系列グラフパネル"""

    def __init__(self):
        super().__init__(expand=True, spacing=0)
        self._frames: List[CanFrame] = []
        self._dbc_loader: Optional[DbcLoader] = None
        self._selected_signals: List[Tuple[int, str]] = []
        self._use_physical = True
        self._use_subplot = False

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
        toolbar = ft.Row(
            controls=[
                self._physical_toggle,
                self._subplot_toggle,
                ft.VerticalDivider(width=1),
                self._save_png_btn,
            ],
            spacing=8,
            alignment=ft.MainAxisAlignment.START,
        )

        self._chart: Optional[PlotlyChart] = None
        self._chart_container = ft.Container(expand=True)
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

        self.controls = [
            ft.Container(content=toolbar, padding=ft.padding.symmetric(horizontal=8, vertical=4)),
            self._placeholder,
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
        self._rebuild_chart()
        self.update()

    def _rebuild_chart(self) -> None:
        """グラフを再構築する"""
        if not self._selected_signals or not self._dbc_loader or not self._frames:
            self.controls = [self.controls[0], self._placeholder]
            return

        # シグナルデータを収集
        signal_data = self._collect_signal_data()
        if not signal_data:
            self.controls = [self.controls[0], self._placeholder]
            return

        # グラフ生成
        if self._use_subplot:
            fig = build_subplot_graph(signal_data, use_physical=self._use_physical)
        else:
            fig = build_overlay_graph(signal_data, use_physical=self._use_physical)

        self._chart = PlotlyChart(figure=fig, expand=True)
        self.controls = [self.controls[0], self._chart]

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
