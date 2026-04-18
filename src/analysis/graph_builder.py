"""Plotly グラフ生成モジュール

シグナルの時系列グラフを Plotly で生成する。
"""

from typing import Callable, Dict, List, Optional, Set, Tuple

import plotly.graph_objects as go
from plotly.subplots import make_subplots

from models.signal_value import SignalValue


# 送信周期の何倍以上の間隔でフレームが来なかった区間を「途絶」として線を切るか
GAP_MULTIPLIER = 5.0

# 強調表示外のシグナルの不透過率（0.0〜1.0, 30% = 半透明）
DIMMED_OPACITY = 0.3


def _prepare_series(
    values: List[SignalValue],
    use_physical: bool,
    cycle_time_ms: Optional[float],
) -> Tuple[List[float], List[Optional[float]]]:
    """SignalValue リストから (times, y_values) を生成する。

    cycle_time_ms が指定されていれば、隣接サンプル間隔がその GAP_MULTIPLIER 倍を
    超える箇所に None を挿入してライン描画を分断する。
    """
    sorted_vals = sorted(values, key=lambda v: v.timestamp)
    if cycle_time_ms is None or cycle_time_ms <= 0:
        times = [v.timestamp for v in sorted_vals]
        y_vals = [
            (v.physical_value if use_physical else v.raw_value)
            for v in sorted_vals
        ]
        return times, y_vals

    gap_threshold_sec = (cycle_time_ms / 1000.0) * GAP_MULTIPLIER
    times: List[float] = []
    y_vals: List[Optional[float]] = []
    prev_t: Optional[float] = None
    for v in sorted_vals:
        if prev_t is not None and (v.timestamp - prev_t) > gap_threshold_sec:
            # ギャップ位置に None を挿入してラインを分断
            times.append(v.timestamp)
            y_vals.append(None)
        times.append(v.timestamp)
        y_vals.append(v.physical_value if use_physical else v.raw_value)
        prev_t = v.timestamp
    return times, y_vals


def _resolve_opacity(name: str, highlighted: Optional[Set[str]]) -> float:
    """強調表示対象集合をもとにシグナルごとの不透過率を決定する"""
    if not highlighted:
        return 1.0
    return 1.0 if name in highlighted else DIMMED_OPACITY


def build_overlay_graph(
    signal_data: Dict[str, List[SignalValue]],
    title: str = "Signal Graph",
    use_physical: bool = True,
    cycle_time_lookup: Optional[Callable[[str], Optional[float]]] = None,
    highlighted: Optional[Set[str]] = None,
) -> go.Figure:
    """複数シグナルを同一グラフ上にオーバーレイ表示する

    Args:
        signal_data: {signal_name: [SignalValue, ...]}
        title: グラフタイトル
        use_physical: True=物理値, False=Raw値
        cycle_time_lookup: signal_name から送信周期(ms)を返す関数。
            None または 0 以下を返した場合はギャップ検出を行わない。
        highlighted: 強調表示するシグナル名の集合。None/空の場合は全シグナルを通常描画。
    """
    fig = go.Figure()

    for name, values in signal_data.items():
        if not values:
            continue
        cycle_ms = cycle_time_lookup(name) if cycle_time_lookup else None
        times, y_vals = _prepare_series(values, use_physical, cycle_ms)
        unit = values[0].unit if values[0].unit else ""
        label = f"{name} [{unit}]" if unit else name
        opacity = _resolve_opacity(name, highlighted)

        # CAN シグナルは離散値のため階段状 (hv) で描画。
        # SVG 書き出し時の描画精度を確保するため Scatter を使用 (Scattergl は WebGL 向け)。
        fig.add_trace(go.Scatter(
            x=times,
            y=y_vals,
            mode="lines",
            line=dict(shape="hv"),
            opacity=opacity,
            name=label,
            connectgaps=False,  # None を挿入したギャップは明示的に分断
            hovertemplate=f"{name}<br>Time: %{{x:.6f}}s<br>Value: %{{y:.4f}} {unit}<extra></extra>",
        ))

    fig.update_layout(
        title=title,
        xaxis_title="Time (s)",
        yaxis_title="Value",
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        dragmode="zoom",
    )

    return fig


def build_subplot_graph(
    signal_data: Dict[str, List[SignalValue]],
    title: str = "Signal Graph",
    use_physical: bool = True,
    cycle_time_lookup: Optional[Callable[[str], Optional[float]]] = None,
    highlighted: Optional[Set[str]] = None,
) -> go.Figure:
    """シグナルごとにサブプロット（縦並び）で表示する"""
    names = list(signal_data.keys())
    if not names:
        return go.Figure()

    fig = make_subplots(
        rows=len(names),
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.02,
        subplot_titles=names,
    )

    for i, name in enumerate(names, start=1):
        values = signal_data[name]
        if not values:
            continue
        cycle_ms = cycle_time_lookup(name) if cycle_time_lookup else None
        times, y_vals = _prepare_series(values, use_physical, cycle_ms)
        unit = values[0].unit if values[0].unit else ""
        opacity = _resolve_opacity(name, highlighted)

        fig.add_trace(
            go.Scatter(
                x=times,
                y=y_vals,
                mode="lines",
                line=dict(shape="hv"),
                opacity=opacity,
                name=name,
                connectgaps=False,
                hovertemplate=f"{name}<br>Time: %{{x:.6f}}s<br>Value: %{{y:.4f}} {unit}<extra></extra>",
            ),
            row=i,
            col=1,
        )
        fig.update_yaxes(title_text=unit if unit else "Value", row=i, col=1)

    fig.update_layout(
        title=title,
        height=max(300 * len(names), 400),
        hovermode="x unified",
        dragmode="zoom",
        showlegend=True,
    )
    fig.update_xaxes(title_text="Time (s)", row=len(names), col=1)

    return fig


def build_bus_load_graph(
    bus_load: Dict[int, List[Tuple[float, float]]],
) -> go.Figure:
    """バスロード時系列グラフを生成する"""
    fig = go.Figure()

    for ch, data in sorted(bus_load.items()):
        times = [d[0] for d in data]
        loads = [d[1] for d in data]
        fig.add_trace(go.Scattergl(
            x=times,
            y=loads,
            mode="lines",
            name=f"Ch {ch}",
            hovertemplate=f"Ch {ch}<br>Time: %{{x:.2f}}s<br>Load: %{{y:.1f}}%<extra></extra>",
        ))

    fig.update_layout(
        title="Bus Load",
        xaxis_title="Time (s)",
        yaxis_title="Bus Load (%)",
        yaxis=dict(range=[0, 100]),
        hovermode="x unified",
        dragmode="zoom",
    )

    return fig
