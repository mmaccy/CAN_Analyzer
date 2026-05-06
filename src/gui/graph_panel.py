"""グラフパネル — Plotly ベースの時系列グラフ表示

選択シグナルの値を時系列グラフで表示する。

描画方式:
- kaleido が Python 3.13 でハングするため、アプリ内には静的プレビュー画像
  (Plotly の to_image に依存しない軽量 matplotlib SVG) を表示する。
- フル機能 (ズーム/パン/リセット/ホバー情報表示等): 「ブラウザで開く」ボタン
  押下時に Plotly 自己完結 HTML を一時ファイルに書き出し、`webbrowser.open`
  で既定ブラウザに表示。
- PNG 保存はグラフの matplotlib 版を使用。
"""

import io
import base64
import flet as ft
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

    # アプリ内 SVG クリック→時刻逆算用の固定マージン (figure 座標)
    # subplots_adjust で軸位置を固定すると、画像内での axes 矩形の比率が
    # 全レンダで一定になるため、クリック座標から時刻への逆変換が安定する。
    _AXES_LEFT_FRAC = 0.08
    _AXES_RIGHT_FRAC = 0.98
    _AXES_TOP_FRAC = 0.95
    _AXES_BOTTOM_FRAC = 0.10

    def __init__(
        self,
        on_request_png_save: Optional[Callable[[go.Figure, Optional[str]], None]] = None,
        on_chart_time_clicked: Optional[Callable[[float], None]] = None,
        on_request_browser_open: Optional[Callable[[go.Figure], Optional[str]]] = None,
    ):
        """
        Args:
            on_request_png_save: PNG 保存ボタンが押された時に呼ばれるコールバック。
                `(figure, signal_name_hint)` の 2 引数で、単一シグナル選択時のみ
                `signal_name_hint` にそのシグナル名が入る（それ以外は None）。
                呼び出し側でファイル名生成と保存ダイアログを扱う。
            on_chart_time_clicked: アプリ内グラフ・ブラウザ Plotly いずれかで
                プロット領域をクリックした際に呼ばれるコールバック。引数は秒単位の時刻。
                main_window 側で TracePanel のジャンプに転送する想定。
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
        self._on_chart_time_clicked = on_chart_time_clicked
        self._on_request_browser_open = on_request_browser_open
        # ASC 全体の時間範囲 (X 軸既定値用)
        self._time_range: Optional[Tuple[float, float]] = None
        # トレース → グラフのカーソル同期 (赤縦線)
        self._cursor_time: Optional[float] = None
        # クリック→時刻逆算用に保持する現在の X 軸データ範囲
        self._click_x_range: Optional[Tuple[float, float]] = None
        # GestureDetector の表示幅 (page.on_resized で更新)
        self._chart_display_width: float = 1000.0

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
        # GestureDetector で包んでクリック→時刻ジャンプを実現 (Plan A)
        self._chart_gesture = ft.GestureDetector(
            content=self._chart_container,
            on_tap_down=self._on_chart_tap_down,
            mouse_cursor=ft.MouseCursor.CLICK,
            expand=True,
        )

        self.controls = [
            ft.Container(content=toolbar, padding=ft.padding.symmetric(horizontal=8, vertical=4)),
            self._legend_container,
            self._chart_gesture,
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

    def add_frames(self, frames: List[CanFrame]) -> None:
        """リアルタイム受信用: フレームをバッチで追記する

        - `_frames` および `_frames_by_id` を更新
        - `_time_range` を新規受信フレームに合わせて拡張
        - 再描画は呼ばない（コスト高のため main_window 側で 1Hz 程度に間引く）
        """
        if not frames:
            return
        self._frames.extend(frames)
        for f in frames:
            self._frames_by_id.setdefault(f.arbitration_id, []).append(f)

        ts_min = min(f.timestamp for f in frames)
        ts_max = max(f.timestamp for f in frames)
        if self._time_range is None:
            self._time_range = (ts_min, ts_max)
        else:
            cur_min, cur_max = self._time_range
            self._time_range = (min(cur_min, ts_min), max(cur_max, ts_max))

    def refresh_live(self) -> None:
        """リアルタイムモード用の再描画。選択シグナルが無ければ何もしない"""
        if not self._selected_signals:
            return
        try:
            self._rebuild_chart()
            self.update()
        except Exception:
            # 再描画失敗時はスキップ（次回ループで再試行）
            pass

    def set_cursor_time(self, t: Optional[float]) -> None:
        """トレース選択行のタイムスタンプをグラフ上のカーソル (赤縦線) として表示

        トレース → グラフ方向のカーソル同期 (Phase 4)。`None` で解除。
        選択シグナルが無い場合や時刻が変わっていない場合は再描画しない。
        """
        if self._cursor_time == t:
            return
        self._cursor_time = t
        if not self._selected_signals or not self._frames:
            return
        try:
            self._rebuild_chart()
            self.update()
        except (AssertionError, RuntimeError):
            # ページ未アタッチ時の update 失敗は無視
            pass

    def notify_chart_width(self, width: float) -> None:
        """グラフ表示幅を更新する (page.on_resized から呼ぶ)

        GestureDetector の `local_x` は表示中のジェスチャ領域に対するピクセル位置だが、
        Flet は実行時サイズを直接取得できないため、ページリサイズ時の幅から逆算する。
        """
        if width > 0:
            self._chart_display_width = float(width)

    def _on_chart_tap_down(self, e) -> None:
        """グラフ上のクリックを時刻に逆算し、コールバックでトレースジャンプを要求"""
        if self._on_chart_time_clicked is None or self._click_x_range is None:
            return
        local_x = float(getattr(e, "local_x", 0) or 0)
        if local_x <= 0 or self._chart_display_width <= 0:
            return
        # GestureDetector の表示幅に対する相対位置 (0..1)
        ratio = local_x / self._chart_display_width
        if ratio < self._AXES_LEFT_FRAC or ratio > self._AXES_RIGHT_FRAC:
            # axes 領域の外（左マージン or 右マージン）はジャンプ対象外
            return
        axes_ratio = (
            (ratio - self._AXES_LEFT_FRAC)
            / (self._AXES_RIGHT_FRAC - self._AXES_LEFT_FRAC)
        )
        t_min, t_max = self._click_x_range
        target_time = t_min + axes_ratio * (t_max - t_min)
        try:
            self._on_chart_time_clicked(target_time)
        except Exception:
            pass

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

    def _value_labels_lookup(self, signal_name: str) -> Optional[Dict[float, str]]:
        """シグナル名から ARXML/DBC Value Table を返す ({Y値: ラベル})"""
        if self._dbc_loader is None:
            return None
        for fid, sname in self._selected_signals:
            if sname == signal_name:
                return self._dbc_loader.get_signal_value_labels(
                    fid, sname, use_physical=self._use_physical,
                )
        return None

    def _rebuild_chart(self) -> None:
        """グラフを再構築する (アプリ内は matplotlib SVG プレビュー)"""
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

            # Plotly figure を生成 (ブラウザ表示・PNG 保存用)
            if self._use_subplot:
                fig = build_subplot_graph(
                    signal_data,
                    use_physical=self._use_physical,
                    cycle_time_lookup=self._cycle_time_lookup,
                    highlighted=self._highlighted or None,
                    x_range=self._time_range,
                    non_negative_lookup=self._non_negative_lookup,
                    value_labels_lookup=self._value_labels_lookup,
                )
            else:
                fig = build_overlay_graph(
                    signal_data,
                    use_physical=self._use_physical,
                    cycle_time_lookup=self._cycle_time_lookup,
                    highlighted=self._highlighted or None,
                    x_range=self._time_range,
                    non_negative_lookup=self._non_negative_lookup,
                    value_labels_lookup=self._value_labels_lookup,
                )

            # ブラウザ表示・PNG保存用にカーソル線も焼き込む
            if self._cursor_time is not None:
                try:
                    fig.add_vline(
                        x=self._cursor_time,
                        line=dict(color="#D32F2F", width=1.2),
                        opacity=0.8,
                    )
                except Exception:
                    # subplot 等で add_vline が複数行に伝播しないケースを許容
                    pass
            self._current_figure = fig  # ブラウザ表示・PNG保存用に保持

            # --- アプリ内プレビュー: matplotlib で軽量 PNG 生成 ---
            svg_data = self._render_matplotlib_preview(signal_data)
            if svg_data:
                _fit = ft.ImageFit.CONTAIN if hasattr(ft, "ImageFit") else ft.BoxFit.CONTAIN
                b64 = base64.b64encode(svg_data).decode("ascii")
                # Flet 0.84+: src_base64 廃止 → src に data URI を設定
                if hasattr(ft.Image, "src_base64"):
                    self._chart = ft.Image(src_base64=b64, fit=_fit, expand=True)
                else:
                    self._chart = ft.Image(src=f"data:image/png;base64,{b64}", fit=_fit, expand=True)
                self._chart_container.content = self._chart
            else:
                # matplotlib が無い場合はテキスト情報 + インストール案内のみ表示
                info_lines = [f"  {name}: {len(vals)} points" for name, vals in signal_data.items()]
                self._chart_container.content = ft.Container(
                    content=ft.Column(
                        controls=[
                            ft.Text(
                                "アプリ内プレビューには matplotlib が必要です",
                                size=13, weight=ft.FontWeight.W_500,
                                color=ft.Colors.ERROR,
                            ),
                            ft.Text(
                                "次のコマンドでインストール後、アプリを再起動してください:",
                                size=11,
                            ),
                            ft.Container(
                                content=ft.Text(
                                    "pip install matplotlib",
                                    size=11, font_family="Consolas",
                                    selectable=True,
                                ),
                                bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
                                padding=6,
                            ),
                            ft.Text(
                                "（暫定対応として「ブラウザで開く」🔗 ボタンでインタラクティブ表示が利用できます）",
                                size=10, italic=True,
                                color=ft.Colors.ON_SURFACE_VARIANT,
                            ),
                            ft.Divider(height=8),
                            ft.Text("選択中シグナル:", size=11, weight=ft.FontWeight.W_500),
                            ft.Text("\n".join(info_lines), size=11, font_family="Consolas"),
                        ],
                        spacing=6,
                    ),
                    padding=12,
                    alignment=ft.Alignment(0, 0),
                    expand=True,
                )

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

    def _render_matplotlib_preview(self, signal_data: Dict[str, List[SignalValue]]) -> Optional[bytes]:
        """matplotlib を使ってアプリ内プレビュー用の PNG バイト列を生成する。

        Plotly 版 (graph_builder) と同じデータ処理ロジック (_prepare_series) を使い、
        階段状 (step) 描画・ギャップ分断を再現する。
        """
        return self._render_matplotlib_png(signal_data, dpi=120)

    def render_png_bytes_for_save(self, dpi: int = 200) -> Optional[bytes]:
        """PNG ファイル保存用の高解像度 PNG バイト列を生成する。

        kaleido が Python 3.13 でハングするため、matplotlib で描画する。
        プレビュー用 (_render_matplotlib_preview) と同じロジックだが DPI を上げる。
        """
        if not self._selected_signals or not self._dbc_loader or not self._frames:
            return None
        signal_data = self._collect_signal_data()
        if not signal_data:
            return None
        return self._render_matplotlib_png(signal_data, dpi=dpi)

    def _render_matplotlib_png(
        self, signal_data: Dict[str, List[SignalValue]], dpi: int = 120,
    ) -> Optional[bytes]:
        """matplotlib で PNG バイト列を生成する共通実装。"""
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            import numpy as np
        except ImportError:
            return None

        from analysis.graph_builder import _prepare_series

        names = list(signal_data.keys())
        n_signals = len(names)
        if n_signals == 0:
            return None

        def _plot_signal(ax, name, vals, color, alpha):
            cycle_ms = self._cycle_time_lookup(name)
            ts, ys = _prepare_series(vals, self._use_physical, cycle_ms)
            # _prepare_series がギャップ箇所に None を返す。
            # matplotlib step(where="post") は NaN を含む点の x 座標まで
            # 前の値の水平線を延長してしまうため、Plotly の connectgaps=False
            # のように完全にラインを分断できない。
            # → None の位置でセグメント分割し、各セグメントを個別に描画する。
            segments: list[tuple[list, list]] = []
            seg_ts: list[float] = []
            seg_ys: list[float] = []
            for t, y in zip(ts, ys):
                if y is None:
                    if seg_ts:
                        segments.append((seg_ts, seg_ys))
                        seg_ts = []
                        seg_ys = []
                else:
                    seg_ts.append(t)
                    seg_ys.append(y)
            if seg_ts:
                segments.append((seg_ts, seg_ys))
            for i, (s_ts, s_ys) in enumerate(segments):
                ax.step(s_ts, np.array(s_ys, dtype=float),
                        where="post", color=color,
                        linewidth=1.0, alpha=alpha,
                        label=name if i == 0 else None)

        def _apply_value_labels(ax, name):
            """Value Table が定義されていれば Y 軸ティックにラベルを設定する"""
            labels = self._value_labels_lookup(name)
            if not labels:
                return
            tick_vals = sorted(labels.keys())
            tick_texts = [f"{v:g} = {labels[v]}" for v in tick_vals]
            ax.set_yticks(tick_vals)
            ax.set_yticklabels(tick_texts)
            # Y 軸範囲を値定義の範囲に合わせて見やすくする
            if len(tick_vals) >= 2:
                span = tick_vals[-1] - tick_vals[0]
                padding = max(span * 0.15, 0.5)
                ax.set_ylim(tick_vals[0] - padding, tick_vals[-1] + padding)
            elif len(tick_vals) == 1:
                ax.set_ylim(tick_vals[0] - 1, tick_vals[0] + 1)

        all_axes: List = []  # カーソル線描画ターゲット（subplot/overlay 共通で参照する）
        if self._use_subplot and n_signals > 1:
            fig, axes = plt.subplots(n_signals, 1, sharex=True, figsize=(10, 2.5 * n_signals))
            if n_signals == 1:
                axes = [axes]
            all_axes = list(axes)
            for i, name in enumerate(names):
                vals = signal_data[name]
                color = _PLOTLY_COLORS[i % len(_PLOTLY_COLORS)]
                alpha = 1.0 if (not self._highlighted or name in self._highlighted) else 0.15
                _plot_signal(axes[i], name, vals, color, alpha)
                unit = vals[0].unit if vals and vals[0].unit else ""
                axes[i].set_ylabel(f"{name}\n[{unit}]" if unit else name, fontsize=8)
                axes[i].tick_params(labelsize=7)
                axes[i].grid(True, alpha=0.3)
                # Value Table があれば Y 軸ラベルに値定義を表示
                _apply_value_labels(axes[i], name)
                # Value Table が無い場合のみ non_negative を適用
                if not self._value_labels_lookup(name):
                    if self._non_negative_lookup(name):
                        axes[i].set_ylim(bottom=0)
            axes[-1].set_xlabel("Time [s]", fontsize=9)
        else:
            fig, ax = plt.subplots(figsize=(10, 5))
            all_axes = [ax]
            for i, name in enumerate(names):
                vals = signal_data[name]
                color = _PLOTLY_COLORS[i % len(_PLOTLY_COLORS)]
                alpha = 1.0 if (not self._highlighted or name in self._highlighted) else 0.15
                _plot_signal(ax, name, vals, color, alpha)
            ax.set_xlabel("Time [s]", fontsize=9)
            ax.set_ylabel("Value" + (" [physical]" if self._use_physical else " [raw]"), fontsize=9)
            ax.legend(fontsize=8, loc="upper right")
            ax.tick_params(labelsize=7)
            ax.grid(True, alpha=0.3)
            # オーバーレイ時: 単一シグナルなら Value Table 適用
            if n_signals == 1:
                _apply_value_labels(ax, names[0])

        # X軸をASC全体の時間範囲に合わせる
        if self._time_range and self._time_range[0] < self._time_range[1]:
            plt.xlim(self._time_range)
            # クリック→時刻逆算用に X 軸範囲を保持
            self._click_x_range = self._time_range
        else:
            # フォールバック: 実データから min/max を算出
            ts_all = [v.timestamp for vals in signal_data.values() for v in vals]
            if ts_all:
                self._click_x_range = (min(ts_all), max(ts_all))

        # トレース → グラフのカーソル同期 (赤縦線)
        if self._cursor_time is not None:
            for cax in all_axes:
                cax.axvline(
                    self._cursor_time,
                    color="#D32F2F",
                    linewidth=0.9,
                    alpha=0.8,
                    zorder=5,
                )

        # クリック→時刻逆算を安定させるため、subplots_adjust で axes 位置を固定する。
        # tight_layout / bbox_inches="tight" は描画ごとに座標が変動するため使わない。
        fig.subplots_adjust(
            left=self._AXES_LEFT_FRAC,
            right=self._AXES_RIGHT_FRAC,
            top=self._AXES_TOP_FRAC,
            bottom=self._AXES_BOTTOM_FRAC,
            hspace=0.25,
        )
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=dpi)
        plt.close(fig)
        buf.seek(0)
        return buf.read()

    def render_png_bytes_for_save(self, dpi: int = 200) -> Optional[bytes]:
        """PNG ファイル保存用の高解像度 PNG バイト列を生成する。

        kaleido が Python 3.13 でハングするため、matplotlib で描画する。
        プレビュー用 (_render_matplotlib_preview) と同じロジックだが DPI を上げる。
        """
        if not self._selected_signals or not self._dbc_loader or not self._frames:
            return None
        signal_data = self._collect_signal_data()
        if not signal_data:
            return None
        return self._render_matplotlib_png(signal_data, dpi=dpi)

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
        """ブラウザで開くボタン: フル機能版 Plotly を既定ブラウザで表示

        on_request_browser_open コールバックが設定されていればローカル HTTP
        サーバ経由で開き、Plotly のクリックを Flet 側に中継する (Plan B)。
        未設定時は従来通り tempfile + file:// URI で開く。
        """
        if self._current_figure is None:
            self._show_snackbar("表示するグラフがありません。先にシグナルを選択してください。")
            return
        try:
            import webbrowser
            from pathlib import Path

            # クリック中継サーバ経由で開く (Plan B)
            if self._on_request_browser_open is not None:
                url = self._on_request_browser_open(self._current_figure)
                if url:
                    webbrowser.open(url)
                    return
                # サーバ起動失敗時は file:// フォールバック

            # フォールバック: tempfile + file:// (クリック→トレースジャンプは効かない)
            import tempfile
            html = self._current_figure.to_html(
                include_plotlyjs="inline",
                full_html=True,
                config=_BROWSER_PLOTLY_CONFIG,
            )
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
