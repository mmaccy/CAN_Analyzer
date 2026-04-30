"""CAN データベース読込・シグナルデコード

cantools ライブラリを使用して DBC / ARXML ファイルからフレーム/シグナル定義を取得し、
CAN フレームデータをデコードする。
"""

import logging
from pathlib import Path
from typing import Dict, List, Optional

import cantools

from models.can_frame import CanFrame
from models.signal_value import SignalValue

_log = logging.getLogger(__name__)

# 対応するデータベースファイル拡張子
SUPPORTED_EXTENSIONS = {".dbc", ".arxml"}


class DbcLoader:
    """CAN データベースの読込・管理・デコード（DBC / ARXML 対応）"""

    def __init__(self):
        self._db = cantools.database.Database()
        self._loaded_files: List[str] = []
        self._custom_files: List[str] = []

    @property
    def loaded_files(self) -> List[str]:
        return list(self._loaded_files)

    @property
    def custom_files(self) -> List[str]:
        return list(self._custom_files)

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

    def load_custom_file(self, file_path: str) -> int:
        """カスタムシグナル定義 JSON を読み込み、DB に注入する。

        Returns:
            追加/置換されたメッセージ数
        """
        from can_parser.custom_definitions import load_custom_definitions
        count = load_custom_definitions(file_path, self._db)
        if count > 0:
            self._custom_files.append(file_path)
        return count

    def export_message_json(self, frame_id: int, output_path: str) -> dict:
        """既存メッセージ定義をカスタム JSON 形式でエクスポートする"""
        from can_parser.custom_definitions import export_message_to_custom_json
        return export_message_to_custom_json(self._db, frame_id, output_path)

    def export_all_json(self, output_path: str) -> int:
        """全メッセージ定義をカスタム JSON 形式でエクスポートする"""
        from can_parser.custom_definitions import export_all_messages_to_json
        return export_all_messages_to_json(self._db, output_path)

    def export_dbc(self, output_path: str) -> int:
        """全メッセージ定義を DBC 形式でエクスポートする"""
        from can_parser.custom_definitions import export_db_as_dbc
        return export_db_as_dbc(self._db, output_path)

    def export_arxml(self, output_path: str) -> int:
        """全メッセージ定義を ARXML 形式でエクスポートする"""
        from can_parser.custom_definitions import export_db_as_arxml
        return export_db_as_arxml(self._db, output_path)

    def clear(self) -> None:
        """読込済み DBC をクリアする"""
        self._db = cantools.database.Database()
        self._loaded_files.clear()
        self._custom_files.clear()

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

    def get_signal_value_labels(
        self, arbitration_id: int, signal_name: str, use_physical: bool = True,
    ) -> Optional[Dict[float, str]]:
        """Value Table (choices) を Y 軸ラベル用に返す。

        Returns:
            {表示Y値: ラベル文字列} の辞書。Value Table 未定義の場合は None。
            use_physical=True の場合、Y 値は physical (raw*scale+offset)。
        """
        try:
            msg = self._db.get_message_by_frame_id(arbitration_id)
        except KeyError:
            return None
        sig = next((s for s in msg.signals if s.name == signal_name), None)
        if sig is None:
            return None
        choices = getattr(sig, "choices", None)
        if not choices:
            return None
        scale = getattr(sig, "scale", 1) or 1
        offset = getattr(sig, "offset", 0) or 0
        result: Dict[float, str] = {}
        for raw_val, label in choices.items():
            if use_physical:
                y_val = float(raw_val) * scale + offset
            else:
                y_val = float(raw_val)
            result[y_val] = str(label)
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
