"""統計パネル — フレーム統計テーブル + バスロードグラフ"""

import flet as ft
try:
    from flet.plotly_chart import PlotlyChart
except ModuleNotFoundError:
    from flet_charts.plotly_chart import PlotlyChart
from typing import List, Optional

from models.can_frame import CanFrame
from models.signal_value import FrameStatistics
from analysis.statistics import compute_frame_statistics, compute_bus_load
from analysis.graph_builder import build_bus_load_graph


class StatisticsPanel(ft.Column):
    """統計情報パネル"""

    def __init__(self):
        super().__init__(expand=True, spacing=0)
        self._frames: List[CanFrame] = []
        self._stats: List[FrameStatistics] = []

        # 統計テーブル
        self._stats_table = ft.DataTable(
            columns=[
                ft.DataColumn(ft.Text("ID", size=12, weight=ft.FontWeight.BOLD)),
                ft.DataColumn(ft.Text("Name", size=12, weight=ft.FontWeight.BOLD)),
                ft.DataColumn(ft.Text("Ch", size=12, weight=ft.FontWeight.BOLD)),
                ft.DataColumn(ft.Text("Count", size=12, weight=ft.FontWeight.BOLD), numeric=True),
                ft.DataColumn(ft.Text("Avg (ms)", size=12, weight=ft.FontWeight.BOLD), numeric=True),
                ft.DataColumn(ft.Text("Min (ms)", size=12, weight=ft.FontWeight.BOLD), numeric=True),
                ft.DataColumn(ft.Text("Max (ms)", size=12, weight=ft.FontWeight.BOLD), numeric=True),
                ft.DataColumn(ft.Text("StdDev (ms)", size=12, weight=ft.FontWeight.BOLD), numeric=True),
            ],
            column_spacing=16,
            data_row_min_height=28,
            data_row_max_height=28,
            heading_row_height=32,
            horizontal_lines=ft.BorderSide(0.5, ft.Colors.OUTLINE),
        )

        self._bus_load_chart_container = ft.Container(height=250)

        self._refresh_btn = ft.ElevatedButton(
            "統計を更新",
            icon=ft.Icons.REFRESH,
            on_click=self._on_refresh,
        )

        table_scroll = ft.Container(
            content=ft.Column(
                controls=[self._stats_table],
                scroll=ft.ScrollMode.AUTO,
                expand=True,
            ),
            expand=True,
        )

        self.controls = [
            ft.Container(
                content=ft.Row([self._refresh_btn], alignment=ft.MainAxisAlignment.START),
                padding=ft.padding.symmetric(horizontal=8, vertical=4),
            ),
            table_scroll,
            ft.Divider(height=1),
            ft.Container(
                content=ft.Text("バスロード", size=12, weight=ft.FontWeight.BOLD),
                padding=ft.padding.only(left=8, top=4),
            ),
            self._bus_load_chart_container,
        ]

    def set_frames(self, frames: List[CanFrame]) -> None:
        """フレームデータを設定する"""
        self._frames = frames

    def refresh(self) -> None:
        """統計を再計算してUI更新"""
        if not self._frames:
            self._stats_table.rows = []
            self._bus_load_chart_container.content = None
            return

        # フレーム統計
        self._stats = compute_frame_statistics(self._frames)
        rows = []
        for s in self._stats:
            id_hex = f"0x{s.arbitration_id:X}"
            rows.append(ft.DataRow(cells=[
                ft.DataCell(ft.Text(id_hex, size=11, font_family="Consolas")),
                ft.DataCell(ft.Text(s.frame_name or "", size=11)),
                ft.DataCell(ft.Text(str(s.channel), size=11)),
                ft.DataCell(ft.Text(str(s.count), size=11)),
                ft.DataCell(ft.Text(f"{s.cycle_avg_ms:.2f}", size=11)),
                ft.DataCell(ft.Text(f"{s.cycle_min_ms:.2f}", size=11)),
                ft.DataCell(ft.Text(f"{s.cycle_max_ms:.2f}", size=11)),
                ft.DataCell(ft.Text(f"{s.cycle_std_ms:.2f}", size=11)),
            ]))
        self._stats_table.rows = rows

        # バスロードグラフ
        bus_load = compute_bus_load(self._frames)
        if bus_load:
            fig = build_bus_load_graph(bus_load)
            self._bus_load_chart_container.content = PlotlyChart(figure=fig, expand=True)
            self._bus_load_chart_container.height = 250
        else:
            self._bus_load_chart_container.content = None

    def _on_refresh(self, e=None) -> None:
        self.refresh()
        self.update()
