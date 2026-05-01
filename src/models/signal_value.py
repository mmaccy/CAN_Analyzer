from dataclasses import dataclass
from typing import Optional


@dataclass
class SignalValue:
    """DBC デコード後のシグナル値

    choice_text は VAL_ テーブル定義 (列挙型) がヒットした場合のラベル文字列。
    ラベル未定義のシグナルでは None。
    """
    signal_name: str
    raw_value: int
    physical_value: float
    unit: str
    timestamp: float
    frame_id: int
    choice_text: Optional[str] = None


@dataclass
class FrameStatistics:
    """フレーム単位の統計情報"""
    arbitration_id: int
    frame_name: Optional[str]
    channel: int
    count: int = 0
    cycle_avg_ms: float = 0.0
    cycle_min_ms: float = 0.0
    cycle_max_ms: float = 0.0
    cycle_std_ms: float = 0.0
    first_timestamp: float = 0.0
    last_timestamp: float = 0.0
