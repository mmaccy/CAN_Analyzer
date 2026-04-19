"""CAN データベース読込・シグナルデコード

cantools ライブラリを使用して DBC / ARXML ファイルからフレーム/シグナル定義を取得し、
CAN フレームデータをデコードする。
"""

from pathlib import Path
from typing import Dict, List, Optional

import cantools

from models.can_frame import CanFrame
from models.signal_value import SignalValue

# 対応するデータベースファイル拡張子
SUPPORTED_EXTENSIONS = {".dbc", ".arxml"}


class DbcLoader:
    """CAN データベースの読込・管理・デコード（DBC / ARXML 対応）"""

    def __init__(self):
        self._db = cantools.database.Database()
        self._loaded_files: List[str] = []

    @property
    def loaded_files(self) -> List[str]:
        return list(self._loaded_files)

    @property
    def messages(self) -> list:
        return self._db.messages

    def load_file(self, file_path: str) -> None:
        """データベースファイルを追加読込する（DBC / ARXML 自動判別、複数ファイル対応）"""
        ext = Path(file_path).suffix.lower()
        if ext == ".dbc":
            self._db.add_dbc_file(file_path)
        elif ext == ".arxml":
            self._db.add_arxml_file(file_path)
        else:
            raise ValueError(
                f"未対応のファイル形式: {ext} "
                f"(対応形式: {', '.join(sorted(SUPPORTED_EXTENSIONS))})"
            )
        self._loaded_files.append(file_path)

    def clear(self) -> None:
        """読込済み DBC をクリアする"""
        self._db = cantools.database.Database()
        self._loaded_files.clear()

    def get_frame_name(self, arbitration_id: int) -> Optional[str]:
        """フレーム ID からフレーム名を取得"""
        try:
            msg = self._db.get_message_by_frame_id(arbitration_id)
            return msg.name
        except KeyError:
            return None

    def get_cycle_time_ms(self, arbitration_id: int) -> Optional[float]:
        """フレーム ID から送信周期 (ms) を取得する。DBC/ARXML で未定義の場合は None"""
        try:
            msg = self._db.get_message_by_frame_id(arbitration_id)
        except KeyError:
            return None
        ct = getattr(msg, "cycle_time", None)
        if ct is None or ct <= 0:
            return None
        return float(ct)

    def get_defined_frame_ids(self) -> set:
        """読込済み DBC/ARXML に定義されている全フレームの arbitration_id 集合を返す"""
        return {msg.frame_id for msg in self._db.messages}

    def is_signal_non_negative(self, arbitration_id: int, signal_name: str) -> bool:
        """指定シグナルが負値を取り得ないかを DBC/ARXML 定義から判定する。

        判定ルール (いずれかを満たせば True):
        - DBC 定義の minimum >= 0
        - unsigned かつ offset >= 0（物理値は必ず offset 以上になる）
        情報が取れない場合は False（= 安全側として auto scale 任せ）。
        """
        try:
            msg = self._db.get_message_by_frame_id(arbitration_id)
        except KeyError:
            return False
        sig = next((s for s in msg.signals if s.name == signal_name), None)
        if sig is None:
            return False
        minimum = getattr(sig, "minimum", None)
        if minimum is not None and minimum >= 0:
            return True
        is_signed = getattr(sig, "is_signed", True)
        offset = getattr(sig, "offset", 0) or 0
        if not is_signed and offset >= 0:
            return True
        return False

    def resolve_frame_names(self, frames: List[CanFrame]) -> None:
        """フレームリストの frame_name を DBC で一括解決する"""
        for frame in frames:
            if frame.frame_name is None:
                frame.frame_name = self.get_frame_name(frame.arbitration_id)

    def decode_frame(self, frame: CanFrame) -> List[SignalValue]:
        """フレームのデータを DBC でデコードし、シグナル値のリストを返す"""
        try:
            msg = self._db.get_message_by_frame_id(frame.arbitration_id)
        except KeyError:
            return []

        try:
            decoded = msg.decode(frame.data, decode_choices=False)
        except Exception:
            return []

        result = []
        for signal in msg.signals:
            if signal.name in decoded:
                phys_val = decoded[signal.name]
                # raw 値の逆算
                if signal.scale and signal.scale != 0:
                    raw = int((phys_val - signal.offset) / signal.scale)
                else:
                    raw = int(phys_val)
                result.append(SignalValue(
                    signal_name=signal.name,
                    raw_value=raw,
                    physical_value=float(phys_val),
                    unit=signal.unit or "",
                    timestamp=frame.timestamp,
                    frame_id=frame.arbitration_id,
                ))
        return result

    def get_signal_info(self, arbitration_id: int) -> List[dict]:
        """指定フレームのシグナル情報を辞書リストで返す"""
        try:
            msg = self._db.get_message_by_frame_id(arbitration_id)
        except KeyError:
            return []

        return [
            {
                "name": s.name,
                "start_bit": s.start,
                "length": s.length,
                "byte_order": s.byte_order,
                "scale": s.scale,
                "offset": s.offset,
                "unit": s.unit or "",
                "minimum": s.minimum,
                "maximum": s.maximum,
            }
            for s in msg.signals
        ]
