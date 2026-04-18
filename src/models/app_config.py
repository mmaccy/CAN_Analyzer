"""アプリ設定ファイル (.canalzcfg) の読み書き

UI 設定（選択シグナル等）を JSON として永続化する。
将来の拡張に備え version フィールドを持つ。
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Tuple


CONFIG_FILE_EXTENSION = "canalzcfg"
CONFIG_VERSION = 1


@dataclass
class AppConfig:
    """アプリ UI 設定のスナップショット"""
    version: int = CONFIG_VERSION
    # 選択中シグナル [(frame_id, signal_name), ...]
    selected_signals: List[Tuple[int, str]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "selected_signals": [
                {"frame_id": int(fid), "signal_name": str(sname)}
                for fid, sname in self.selected_signals
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AppConfig":
        version = int(data.get("version", CONFIG_VERSION))
        signals_raw = data.get("selected_signals", []) or []
        selected: List[Tuple[int, str]] = []
        for item in signals_raw:
            if not isinstance(item, dict):
                continue
            fid = item.get("frame_id")
            sname = item.get("signal_name")
            if fid is None or sname is None:
                continue
            selected.append((int(fid), str(sname)))
        return cls(version=version, selected_signals=selected)


def save_config(config: AppConfig, path: str) -> None:
    """設定を JSON として書き出す"""
    Path(path).write_text(
        json.dumps(config.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_config(path: str) -> AppConfig:
    """JSON から設定を読み込む"""
    text = Path(path).read_text(encoding="utf-8")
    data = json.loads(text)
    return AppConfig.from_dict(data)
