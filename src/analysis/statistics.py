"""統計計算モジュール

フレーム周期・バスロード等の統計情報を算出する。
"""

from collections import defaultdict
from typing import Dict, List, Optional, Tuple

import numpy as np

from models.can_frame import CanFrame, DLC_TO_LENGTH
from models.signal_value import FrameStatistics


def compute_frame_statistics(
    frames: List[CanFrame],
    time_range: Optional[Tuple[float, float]] = None,
) -> List[FrameStatistics]:
    """フレームリストから統計情報を算出する"""
    # (id, channel) ごとにタイムスタンプを集約
    ts_map: Dict[Tuple[int, int], List[float]] = defaultdict(list)
    name_map: Dict[Tuple[int, int], Optional[str]] = {}

    for f in frames:
        if time_range:
            if f.timestamp < time_range[0] or f.timestamp > time_range[1]:
                continue
        key = (f.arbitration_id, f.channel)
        ts_map[key].append(f.timestamp)
        if key not in name_map:
            name_map[key] = f.frame_name

    result = []
    for (arb_id, ch), timestamps in sorted(ts_map.items()):
        timestamps.sort()
        count = len(timestamps)

        if count >= 2:
            diffs = np.diff(timestamps) * 1000.0  # ms
            cycle_avg = float(np.mean(diffs))
            cycle_min = float(np.min(diffs))
            cycle_max = float(np.max(diffs))
            cycle_std = float(np.std(diffs))
        else:
            cycle_avg = cycle_min = cycle_max = cycle_std = 0.0

        result.append(FrameStatistics(
            arbitration_id=arb_id,
            frame_name=name_map.get((arb_id, ch)),
            channel=ch,
            count=count,
            cycle_avg_ms=cycle_avg,
            cycle_min_ms=cycle_min,
            cycle_max_ms=cycle_max,
            cycle_std_ms=cycle_std,
            first_timestamp=timestamps[0],
            last_timestamp=timestamps[-1],
        ))

    return result


def compute_bus_load(
    frames: List[CanFrame],
    interval_sec: float = 1.0,
) -> Dict[int, List[Tuple[float, float]]]:
    """チャンネルごとのバスロード (%) を時間帯別に算出する

    Returns:
        {channel: [(time_sec, load_percent), ...]}
    """
    if not frames:
        return {}

    # フレームを時間順にソート
    sorted_frames = sorted(frames, key=lambda f: f.timestamp)
    t_start = sorted_frames[0].timestamp
    t_end = sorted_frames[-1].timestamp

    # チャンネルごとにビット数を時間帯別に集計
    ch_bits: Dict[int, Dict[int, int]] = defaultdict(lambda: defaultdict(int))
    channels = set()

    for f in sorted_frames:
        channels.add(f.channel)
        bucket = int((f.timestamp - t_start) / interval_sec)
        # ビット数の推定: (ヘッダ + データ) bits
        if f.is_fd:
            # CAN FD: ~20 header bits + data * 8
            bits = 160 + f.data_length * 8
        else:
            # Classic CAN: ~47 header bits + data * 8 + ~25 trailing bits
            bits = 72 + f.data_length * 8
        ch_bits[f.channel][bucket] += bits

    # 各チャンネルのバスロードを算出
    # 標準ビットレート 500kbps を仮定 (1秒間に 500,000 bits)
    max_bits_per_interval = 500_000 * interval_sec
    result: Dict[int, List[Tuple[float, float]]] = {}

    num_buckets = int((t_end - t_start) / interval_sec) + 1
    for ch in sorted(channels):
        loads = []
        for b in range(num_buckets):
            t = t_start + b * interval_sec
            bits = ch_bits[ch].get(b, 0)
            load = (bits / max_bits_per_interval) * 100.0
            loads.append((t, min(load, 100.0)))
        result[ch] = loads

    return result
