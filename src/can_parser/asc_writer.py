"""ASC ファイルエクスポート

フィルタ済みフレームを ASC 形式で書き出す。
元ファイルのヘッダ情報を保持する。

リアルタイム受信時のライブログ書込用に format_frame_as_asc / write_default_header
も提供する。これらは Vector ASC のサブセット（必須フィールドのみ）を出力するが、
asc_parser の regex でラウンドトリップ可能な形式となっている。
"""

from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Set, TextIO

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


def format_frame_as_asc(frame: CanFrame) -> str:
    """CanFrame を ASC 1 行に整形する（リアルタイム受信ログ用）

    Vector ASC のサブセット（asc_parser がパース可能な必須フィールドのみ）を
    出力する。Length / BitCount / bit_timing 等の情報フィールドは省略。
    """
    if frame.is_fd:
        # CAN FD: "{ts} CANFD   {ch} {dir}        {id} {name}  {brs} {esi} {dlc:x}  {dlen} {data}"
        name = frame.frame_name or ""
        return (
            f"{frame.timestamp:11.6f} CANFD  "
            f"{frame.channel:2d} {frame.dir_str}        "
            f"{frame.id_hex:>8s} {name:<24s} "
            f"{1 if frame.brs else 0} {1 if frame.esi else 0} "
            f"{frame.dlc:x}  {frame.data_length:2d} "
            f"{frame.data_hex}"
        )
    # Classic CAN: "{ts} {ch}  {id}        {dir}   d {dlc} {data}"
    return (
        f"{frame.timestamp:11.6f} {frame.channel:d}  "
        f"{frame.id_hex:<8s} {frame.dir_str}   d "
        f"{frame.dlc:d} {frame.data_hex}"
    )


def write_default_header(out: TextIO, fd: bool = True) -> None:
    """リアルタイムログ用の最小 ASC ヘッダを書き出す"""
    now = datetime.now()
    # Vector の "date" 行は曜略+月略+日 時:分:秒 年 形式
    out.write(f"date {now.strftime('%a %b %d %I:%M:%S.%f %p %Y')[:-3]}\n")
    out.write("base hex  timestamps absolute\n")
    out.write("internal events logged\n")
    out.write("// version 13.0.0\n")
    out.write(f"Begin TriggerBlock {now.strftime('%a %b %d %I:%M:%S.%f %p %Y')[:-3]}\n")
    out.write("   0.000000 Start of measurement\n")


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
