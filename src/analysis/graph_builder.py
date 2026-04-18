"""Plotly グラフ生成モジュール

シグナルの時系列グラフを Plotly で生成する。
"""

from typing import Dict, List, Optional, Tuple

import plotly.graph_objects as go
from plotly.subplots import make_subplots

from models.signal_value import SignalValue


def build_overlay_graph(
    signal_data: Dict[str, List[SignalValue]],
    title: str = "Signal Graph",
    use_physical: bool = True,
) -> go.Figure:
    """複数シグナルを同一グラフ上にオーバーレイ表示する

    Args:
        signal_data: {signal_name: [SignalValue, ...]}
        title: グラフタイトル
        use_physical: True=物理値, False=Raw値
    """
    fig = go.Figure()

    for name, values in signal_data.items():
        if not values:
            continue
        times = [v.timestamp for v in values]
        y_vals = [v.physical_value if use_physical else v.raw_value for v in values]
        unit = values[0].unit if values[0].unit else ""
        label = f"{name} [{unit}]" if unit else name

        fig.add_trace(go.Scattergl(
            x=times,
            y=y_vals,
            mode="lines",
            name=label,
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
        times = [v.timestamp for v in values]
        y_vals = [v.physical_value if use_physical else v.raw_value for v in values]
        unit = values[0].unit if values[0].unit else ""

        fig.add_trace(
            go.Scattergl(
                x=times,
                y=y_vals,
                mode="lines",
                name=name,
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
