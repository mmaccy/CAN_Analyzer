from dataclasses import dataclass, field
from typing import Optional


# CAN FD DLC code → actual data length mapping
DLC_TO_LENGTH = {
    0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 7: 7, 8: 8,
    9: 12, 10: 16, 11: 20, 12: 24, 13: 32, 14: 48, 15: 64,
}


@dataclass
class AscHeader:
    """ASC ファイルヘッダ情報"""
    date: str = ""
    base: str = "hex"
    timestamps: str = "absolute"
    version: str = ""
    measurement_uuid: str = ""
    trigger_block_date: str = ""
    raw_header_lines: list = field(default_factory=list)


@dataclass(slots=True)
class CanFrame:
    """パース済み CAN/CAN FD フレーム

    slots により属性ディクショナリを廃し、1M 行規模での常駐メモリを削減している。
    raw_line は iter_frames ストリームで元 ASC 行を返すため保持するが、
    load_all_frames でキャッシュする際には破棄される（エクスポートは元ファイルを
    再ストリームするため raw_line は常駐不要）。
    """
    timestamp: float
    channel: int
    arbitration_id: int
    is_extended_id: bool
    is_fd: bool
    is_rx: bool
    dlc: int
    data_length: int
    data: bytes
    frame_name: Optional[str] = None
    brs: Optional[bool] = None
    esi: Optional[bool] = None
    raw_line: str = ""
    file_offset: int = 0

    @property
    def id_hex(self) -> str:
        """フレーム ID を hex 文字列で返す"""
        suffix = "x" if self.is_extended_id else ""
        return f"{self.arbitration_id:X}{suffix}"

    @property
    def type_str(self) -> str:
        return "CANFD" if self.is_fd else "CAN"

    @property
    def dir_str(self) -> str:
        return "Rx" if self.is_rx else "Tx"

    @property
    def data_hex(self) -> str:
        """データを hex 文字列で返す（スペース区切り）"""
        return " ".join(f"{b:02X}" for b in self.data)
