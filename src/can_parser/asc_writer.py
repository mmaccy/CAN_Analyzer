"""ASC ファイルエクスポート

フィルタ済みフレームを ASC 形式で書き出す。
元ファイルのヘッダ情報を保持する。
"""

from pathlib import Path
from typing import Callable, Iterable, List, Optional, Set

from models.can_frame import AscHeader, CanFrame
from can_parser.asc_parser import iter_frames, parse_header


def export_filtered(
    source_path: str,
    output_path: str,
    frame_ids: Optional[Set[int]] = None,
    frame_names: Optional[Set[str]] = None,
    channels: Optional[Set[int]] = None,
    time_range: Optional[tuple] = None,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> int:
    """条件に合致するフレームを ASC ファイルとしてエクスポートする

    Args:
        source_path: 元 ASC ファイルパス
        output_path: 出力先ファイルパス
        frame_ids: 抽出するフレーム ID のセット (None=全て)
        frame_names: 抽出するフレーム名のセット (None=全て)
        channels: 抽出するチャンネルのセット (None=全て)
        time_range: (start_sec, end_sec) 時間範囲 (None=全て)
        progress_callback: (bytes_read, total_bytes) 進捗コールバック

    Returns:
        エクスポートしたフレーム数
    """
    header = parse_header(source_path)

    exported = 0
    with open(output_path, "w", encoding="utf-8") as out:
        # ヘッダ書き出し
        for hline in header.raw_header_lines:
            out.write(hline + "\n")

        for frame in iter_frames(source_path, progress_callback):
            if not _matches_filter(frame, frame_ids, frame_names, channels, time_range):
                continue
            out.write(frame.raw_line + "\n")
            exported += 1

    return exported


def _matches_filter(
    frame: CanFrame,
    frame_ids: Optional[Set[int]],
    frame_names: Optional[Set[str]],
    channels: Optional[Set[int]],
    time_range: Optional[tuple],
) -> bool:
    """フレームがフィルタ条件に合致するか判定 (AND 条件)"""
    if frame_ids is not None and frame.arbitration_id not in frame_ids:
        return False
    if frame_names is not None:
        if frame.frame_name is None or frame.frame_name not in frame_names:
            return False
    if channels is not None and frame.channel not in channels:
        return False
    if time_range is not None:
        t_start, t_end = time_range
        if frame.timestamp < t_start or frame.timestamp > t_end:
            return False
    return True
