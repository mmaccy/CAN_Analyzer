"""グラフパネル — Plotly ベースの時系列グラフ表示

選択シグナルの値を時系列グラフで表示する。

描画方式:
- アプリ内: `PlotlyChart` (kaleido 経由の静的 SVG)。軽量で常時表示に向く。
  表示されるだけの ModeBar 残骸は `modebar=dict(remove=['all'])` で抑止。
- フル機能 (ズーム/パン/リセット/ホバー情報表示等): 「ブラウザで開く」ボタン
  押下時に Plotly 自己完結 HTML を一時ファイルに書き出し、`webbrowser.open`
  で既定ブラウザに表示。
  Windows デスクトップの Flet WebView が未対応 (0.84 時点) のため、この構成を採用。
- PNG 保存は引き続き `fig.write_image` (kaleido) で高解像度書き出し。
"""

import flet as ft
try:
    from flet.plotly_chart import PlotlyChart
except ModuleNotFoundError:
    from flet_charts.plotly_chart import PlotlyChart
from typing import Callable, Dict, List, Optional, Set, Tuple

import plotly.graph_objects as go

from models.can_frame import CanFrame
from models.signal_value import SignalValue
from can_parser.dbc_loader import DbcLoader
from analysis.graph_builder import build_overlay_graph, build_subplot_graph


# Plotly の既定カラーシーケンス（凡例 UI のマーカー色と合わせる）
_PLOTLY_COLORS = [
    "#636EFA", "#EF553B", "#00CC96", "#AB63FA", "#FFA15A",
    "#19D3F3", "#FF6692", "#B6E880", "#FF97FF", "#FECB52",
]


# ブラウザに吐き出すインタラクティブ HTML 用の Plotly 設定
_BROWSER_PLOTLY_CONFIG = {
    "displayModeBar": True,
    "displaylogo": False,
    "responsive": True,
    "modeBarButtonsToRemove": ["sendDataToCloud"],
}


class GraphPanel(ft.Column):
    """時系列グラフパネル"""

    def __init__(
        self,
        on_request_png_save: Optional[Callable[[go.Figure, Optional[str]], None]] = None,
    ):
        """
        Args:
            on_request_png_save: PNG 保存ボタンが押された時に呼ばれるコールバック。
                `(figure, signal_name_hint)` の 2 引数で、単一シグナル選択時のみ
                `signal_name_hint` にそのシグナル名が入る（それ以外は None）。
                呼び出し側でファイル名生成と保存ダイアログを扱う。
        """
        super().__init__(expand=True, spacing=0)
        self._frames: List[CanFrame] = []
        # frame_id → そのID の CanFrame リスト（大容量ログでも対象 frame のみ走査できる）
        self._frames_by_id: Dict[int, List[CanFrame]] = {}
        self._dbc_loader: Optional[DbcLoader] = None
        self._selected_signals: List[Tuple[int, str]] = []
        self._use_physical = True
        self._use_subplot = False
        # 強調表示中のシグナル名集合。空なら全シグナルを通常描画
        self._highlighted: Set[str] = set()
        # 現在のグラフ figure（PNG エクスポート用に保持）
        self._current_figure: Optional[go.Figure] = None
        self._on_request_png_save = on_request_png_save
        # ASC 全体の時間範囲 (X 軸既定値用)
        self._time_range: Optional[Tuple[float, float]] = None

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
        self._open_browser_btn = ft.IconButton(
            ft.Icons.OPEN_IN_NEW,
            tooltip="ブラウザで開く（インタラクティブ・ホバー情報表示）",
            on_click=self._on_open_in_browser,
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
                self._open_browser_btn,
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

        # 静的 SVG チャート領域 (kaleido 経由)
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
        # frame_id → frames の転置インデックスを作成。3.4M 行規模でも選択 frame のみ
        # 走査できるよう事前に arbitration_id でバケット化しておく。
        self._frames_by_id = {}
        for f in frames:
            self._frames_by_id.setdefault(f.arbitration_id, []).append(f)
        self._dbc_loader = dbc_loader
        # ASC 全体の時間範囲。グラフ X 軸の既定表示範囲として使用する
        # (ASC は通常時刻順だが、念のため min/max で計算)。
        if frames:
            ts_first = frames[0].timestamp
            ts_last = frames[-1].timestamp
            self._time_range = (min(ts_first, ts_last), max(ts_first, ts_last))
        else:
            self._time_range = None
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

    def _non_negative_lookup(self, signal_name: str) -> bool:
        """シグナルが負値を取らないかを DBC/ARXML 定義から判定する"""
        if self._dbc_loader is None:
            return False
        for fid, sname in self._selected_signals:
            if sname == signal_name:
                return self._dbc_loader.is_signal_non_negative(fid, sname)
        return False

    def _rebuild_chart(self) -> None:
        """グラフを再構築する (アプリ内は静的 SVG)"""
        if not self._selected_signals or not self._dbc_loader or not self._frames:
            self._chart = None
            self._current_figure = None
            self._chart_container.content = self._placeholder.content
            self._legend_container.visible = False
            self._reset_highlight_btn.visible = False
            return

        try:
            signal_data = self._collect_signal_data()
            if not signal_data:
                self._chart = None
                self._current_figure = None
                self._chart_container.content = ft.Container(
                    content=ft.Text(
                        "選択シグナルに該当するデータがログ中に見つかりませんでした",
                        size=13, italic=True, text_align=ft.TextAlign.CENTER,
                    ),
                    alignment=ft.Alignment(0, 0),
                    expand=True,
                )
                self._legend_container.visible = False
                self._reset_highlight_btn.visible = False
                return

            if self._use_subplot:
                fig = build_subplot_graph(
                    signal_data,
                    use_physical=self._use_physical,
                    cycle_time_lookup=self._cycle_time_lookup,
                    highlighted=self._highlighted or None,
                    x_range=self._time_range,
                    non_negative_lookup=self._non_negative_lookup,
                )
            else:
                fig = build_overlay_graph(
                    signal_data,
                    use_physical=self._use_physical,
                    cycle_time_lookup=self._cycle_time_lookup,
                    highlighted=self._highlighted or None,
                    x_range=self._time_range,
                    non_negative_lookup=self._non_negative_lookup,
                )

            # 静的 SVG では操作不能な ModeBar は非表示にする
            # （インタラクティブ操作は「ブラウザで開く」ボタンで提供）
            fig.update_layout(modebar=dict(remove=["all"]))

            self._current_figure = fig  # PNG 保存・ブラウザ表示用に保持
            self._chart = PlotlyChart(figure=fig, expand=True)
            self._chart_container.content = self._chart

            self._rebuild_legend(list(signal_data.keys()))
        except Exception as ex:
            import traceback
            tb = traceback.format_exc()
            self._chart = None
            self._current_figure = None
            self._chart_container.content = ft.Container(
                content=ft.Column(
                    controls=[
                        ft.Text("グラフ生成エラー", size=14, weight=ft.FontWeight.BOLD, color=ft.Colors.ERROR),
                        ft.Text(f"{type(ex).__name__}: {ex}", size=12, color=ft.Colors.ERROR),
                        ft.Container(
                            content=ft.Text(tb, size=10, font_family="Consolas"),
                            padding=8,
                            bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
                        ),
                    ],
                    scroll=ft.ScrollMode.AUTO,
                    spacing=6,
                ),
                padding=12,
                expand=True,
            )
            self._legend_container.visible = False
            self._reset_highlight_btn.visible = False

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
        """選択シグナルに対応するデータを収集

        対象 frame_id に属するフレームのみを走査することで、大容量ログ
        (数百万行) でも選択シグナル数 × そのフレームの出現回数に比例する
        計算量に抑える。
        """
        if not self._dbc_loader:
            return {}

        # frame_id → [signal_name, ...] のマップ
        target_map: Dict[int, List[str]] = {}
        for fid, sname in self._selected_signals:
            target_map.setdefault(fid, []).append(sname)

        result: Dict[str, List[SignalValue]] = {
            sname: [] for _, sname in self._selected_signals
        }

        decode = self._dbc_loader.decode_frame
        for fid, target_signals in target_map.items():
            target_set = set(target_signals)
            frames = self._frames_by_id.get(fid, ())
            for frame in frames:
                for sv in decode(frame):
                    if sv.signal_name in target_set:
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
        """PNG 保存ボタン: 現在の figure をコールバック経由で保存要求"""
        if self._current_figure is None:
            self._show_snackbar("保存するグラフがありません。先にシグナルを選択してください。")
            return
        if self._on_request_png_save is None:
            # MainWindow から未接続（テスト等）
            return
        # 単一シグナル選択時のみヒントとしてシグナル名を渡す
        signal_hint: Optional[str] = None
        if len(self._selected_signals) == 1:
            signal_hint = self._selected_signals[0][1]
        self._on_request_png_save(self._current_figure, signal_hint)

    def _on_open_in_browser(self, e) -> None:
        """ブラウザで開くボタン: フル機能版 Plotly を既定ブラウザで表示"""
        if self._current_figure is None:
            self._show_snackbar("表示するグラフがありません。先にシグナルを選択してください。")
            return
        try:
            import tempfile
            import webbrowser
            from pathlib import Path

            # インタラクティブ HTML を生成 (plotly.js を CDN ではなく inline で同梱)
            html = self._current_figure.to_html(
                include_plotlyjs="inline",
                full_html=True,
                config=_BROWSER_PLOTLY_CONFIG,
            )
            # 一時ファイルに書き出してブラウザで開く
            tmp = tempfile.NamedTemporaryFile(
                prefix="can_graph_", suffix=".html", delete=False, mode="w", encoding="utf-8"
            )
            tmp.write(html)
            tmp.close()
            webbrowser.open(Path(tmp.name).as_uri())
        except Exception as ex:
            self._show_snackbar(f"ブラウザ表示エラー: {ex}")

    def _show_snackbar(self, message: str) -> None:
        page = getattr(self, "page", None)
        if page is None:
            return
        try:
            sb = ft.SnackBar(ft.Text(message), duration=3000)
            if hasattr(page, "snack_bar"):
                page.snack_bar = sb
                page.snack_bar.open = True
            else:
                page.show_dialog(sb)
            page.update()
        except Exception:
            pass
