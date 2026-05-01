"""アプリケーション共通ロガー

サイレントに握り潰されがちな例外をファイルに残し、
ユーザ向けには簡潔なメッセージ、開発者向けにはスタックトレースを残す。

設置場所:
- Windows: %APPDATA%/can_analyzer/app.log
- それ以外: ~/.can_analyzer/app.log
- 失敗時: <tempdir>/can_analyzer.log にフォールバック

使用例:
    from utils.logger import get_logger
    log = get_logger(__name__)
    try:
        ...
    except Exception:
        log.exception("ASC パース中に例外発生")
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import tempfile
from pathlib import Path
from typing import Optional


_INITIALIZED = False
_LOG_PATH: Optional[Path] = None


def _resolve_log_dir() -> Path:
    """ログ保存先ディレクトリを決定する"""
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "can_analyzer"
    home = Path.home()
    return home / ".can_analyzer"


def setup_logging(level: int = logging.INFO) -> Path:
    """ログシステムを初期化する。アプリ起動時に 1 度だけ呼ぶ。

    Returns:
        ログファイルパス。書き込みに失敗した場合は <tempdir>/can_analyzer.log。
    """
    global _INITIALIZED, _LOG_PATH
    if _INITIALIZED and _LOG_PATH is not None:
        return _LOG_PATH

    log_dir = _resolve_log_dir()
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "app.log"
    except Exception:
        log_path = Path(tempfile.gettempdir()) / "can_analyzer.log"

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger("can_analyzer")
    root.setLevel(logging.DEBUG)
    # 重複ハンドラ追加防止
    if not any(getattr(h, "_app_handler", False) for h in root.handlers):
        try:
            file_handler = logging.handlers.RotatingFileHandler(
                str(log_path), maxBytes=2_000_000, backupCount=3, encoding="utf-8",
            )
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(formatter)
            file_handler._app_handler = True  # type: ignore[attr-defined]
            root.addHandler(file_handler)
        except Exception:
            # ファイルが開けない場合でも stderr ログだけは残す
            pass

        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setLevel(level)
        stream_handler.setFormatter(formatter)
        stream_handler._app_handler = True  # type: ignore[attr-defined]
        root.addHandler(stream_handler)

    _LOG_PATH = log_path
    _INITIALIZED = True
    return log_path


def get_logger(name: str) -> logging.Logger:
    """モジュール用ロガーを返す。setup_logging が未呼出でも動作する。"""
    if not _INITIALIZED:
        setup_logging()
    # "can_analyzer" 配下にぶら下げる（root logger を汚さない）
    if name.startswith("can_analyzer."):
        return logging.getLogger(name)
    return logging.getLogger(f"can_analyzer.{name}")


def get_log_path() -> Optional[Path]:
    """現在のログファイルパスを返す。setup 未実行なら None。"""
    return _LOG_PATH
