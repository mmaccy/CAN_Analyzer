"""ASC ファイルストリーミングパーサ

Vector ASC 形式 (CANoe 17.x) の Classic CAN / CAN FD ログを
行単位でストリーミングパースする。GB 級ファイルに対応。
"""

import re
from pathlib import Path
from typing import Generator, List, Optional, Callable, Tuple

from models.can_frame import AscHeader, CanFrame, DLC_TO_LENGTH

# ---------- regex patterns ----------

# CAN FD line — 固定幅・可変幅の両方に対応
# フィールド間のスペースが省略される場合があるため \s* を使用する箇所あり
#   例: "0.001951CANFD" (タイムスタンプ直結), "2Rx" (ch直結dir), "158TM_158" (ID直結name)
_RE_CANFD = re.compile(
    r"^\s*"
    r"(?P<ts>\d+\.\d+)\s*"              # timestamp (CANFD と直結の場合あり)
    r"CANFD\s+"
    r"(?P<ch>\d+)\s*"                    # channel (dir と直結の場合あり)
    r"(?P<dir>Rx|Tx)\s+"
    r"(?P<id>[0-9A-Fa-f]+x?)"           # hex ID + optional extended flag
    r"(?P<name>[A-Za-z_]\S*)?\s+"        # optional frame name (starts with letter/_)
    r"(?P<brs>[01])\s+"
    r"(?P<esi>[01])\s+"
    r"(?P<dlc>[0-9A-Fa-f]+)\s+"
    r"(?P<dlen>\d+)\s+"
    r"(?P<data>.+)"                      # data + trailing (parsed by _parse_data_bytes)
)

# Classic CAN line — 同様に可変幅対応
_RE_CLASSIC = re.compile(
    r"^\s*"
    r"(?P<ts>\d+\.\d+)\s*"
    r"(?P<ch>\d+)\s+"
    r"(?P<id>[0-9A-Fa-f]+x?)\s+"
    r"(?P<dir>Rx|Tx)\s+"
    r"d\s+"
    r"(?P<dlc>\d+)\s+"
    r"(?P<data>.+)"
)


def _parse_id(id_str: str) -> Tuple[int, bool]:
    """hex ID 文字列をパースし、(arbitration_id, is_extended) を返す"""
    is_ext = id_str.endswith("x")
    hex_str = id_str.rstrip("x")
    return int(hex_str, 16), is_ext


def _parse_data_bytes(data_str: str, expected_len: int) -> bytes:
    """スペース区切り hex データをバイト列に変換"""
    tokens = data_str.strip().split()
    result = bytearray()
    for t in tokens[:expected_len]:
        val = int(t, 16)
        if val > 0xFF:
            break
        result.append(val)
    return bytes(result)


def _parse_canfd_line(line: str, offset: int) -> Optional[CanFrame]:
    """CAN FD 行をパース"""
    m = _RE_CANFD.match(line)
    if not m:
        return None

    arb_id, is_ext = _parse_id(m.group("id"))
    dlc_code = int(m.group("dlc"), 16)
    data_length = int(m.group("dlen"))
    name_raw = m.group("name")
    name = name_raw.strip() if name_raw else ""

    return CanFrame(
        timestamp=float(m.group("ts")),
        channel=int(m.group("ch")),
        arbitration_id=arb_id,
        is_extended_id=is_ext,
        is_fd=True,
        is_rx=(m.group("dir") == "Rx"),
        dlc=dlc_code,
        data_length=data_length,
        data=_parse_data_bytes(m.group("data"), data_length),
        frame_name=name if name else None,
        brs=(m.group("brs") == "1"),
        esi=(m.group("esi") == "1"),
        raw_line=line.rstrip("\n"),
        file_offset=offset,
    )


def _parse_classic_line(line: str, offset: int) -> Optional[CanFrame]:
    """Classic CAN 行をパース"""
    m = _RE_CLASSIC.match(line)
    if not m:
        return None

    arb_id, is_ext = _parse_id(m.group("id"))
    dlc = int(m.group("dlc"))

    return CanFrame(
        timestamp=float(m.group("ts")),
        channel=int(m.group("ch")),
        arbitration_id=arb_id,
        is_extended_id=is_ext,
        is_fd=False,
        is_rx=(m.group("dir") == "Rx"),
        dlc=dlc,
        data_length=dlc,
        data=_parse_data_bytes(m.group("data"), dlc),
        raw_line=line.rstrip("\n"),
        file_offset=offset,
    )


def parse_header(file_path: str) -> AscHeader:
    """ASC ファイルのヘッダ部分のみをパースする"""
    header = AscHeader()
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue

            # ヘッダ行の判定
            if stripped.startswith("date "):
                header.date = stripped[5:]
                header.raw_header_lines.append(line.rstrip("\n"))
            elif stripped.startswith("base "):
                header.base = stripped[5:].strip()
                header.raw_header_lines.append(line.rstrip("\n"))
            elif stripped.startswith("timestamps "):
                header.timestamps = stripped[11:].strip()
                header.raw_header_lines.append(line.rstrip("\n"))
            elif stripped == "internal events logged":
                header.raw_header_lines.append(line.rstrip("\n"))
            elif stripped.startswith("// version"):
                header.version = stripped.split("version", 1)[1].strip()
                header.raw_header_lines.append(line.rstrip("\n"))
            elif stripped.startswith("// Measurement UUID:"):
                header.measurement_uuid = stripped.split("UUID:", 1)[1].strip()
                header.raw_header_lines.append(line.rstrip("\n"))
            elif stripped.startswith("Begin TriggerBlock"):
                header.trigger_block_date = stripped[len("Begin TriggerBlock"):].strip()
                header.raw_header_lines.append(line.rstrip("\n"))
            elif stripped.startswith("//"):
                header.raw_header_lines.append(line.rstrip("\n"))
            else:
                # データ行に到達したら終了
                break
    return header


def iter_frames(
    file_path: str,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> Generator[CanFrame, None, None]:
    """ASC ファイルのフレームをストリーミングで yield する

    Args:
        file_path: ASC ファイルパス
        progress_callback: (bytes_read, total_bytes) を受け取るコールバック
    """
    path = Path(file_path)
    total_size = path.stat().st_size
    bytes_read = 0
    report_interval = max(total_size // 200, 65536)  # ~0.5% ごとに通知
    last_report = 0

    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            offset = bytes_read
            bytes_read += len(line.encode("utf-8", errors="replace"))

            # 進捗通知
            if progress_callback and (bytes_read - last_report) >= report_interval:
                progress_callback(bytes_read, total_size)
                last_report = bytes_read

            stripped = line.strip()
            if not stripped:
                continue

            # ヘッダ・特殊行はスキップ
            if (stripped.startswith("date ") or stripped.startswith("base ")
                    or stripped.startswith("timestamps ")
                    or stripped == "internal events logged"
                    or stripped.startswith("//")
                    or stripped.startswith("Begin TriggerBlock")
                    or stripped.startswith("End TriggerBlock")
                    or "Start of measurement" in stripped):
                continue

            # CAN FD → Classic CAN の順で試行
            frame = None
            try:
                if "CANFD" in line:
                    frame = _parse_canfd_line(line, offset)
                if frame is None:
                    frame = _parse_classic_line(line, offset)
            except (ValueError, OverflowError):
                # パースできない行はスキップ
                continue

            if frame is not None:
                yield frame

    # 最終進捗
    if progress_callback:
        progress_callback(total_size, total_size)


def load_all_frames(
    file_path: str,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> List[CanFrame]:
    """ASC ファイルの全フレームをリストで返す（小〜中規模ファイル向け）"""
    return list(iter_frames(file_path, progress_callback))
