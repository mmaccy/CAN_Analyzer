"""CAN FD Log Analyzer — Flet エントリポイント"""

import sys
from pathlib import Path

# src ディレクトリをパスに追加
src_dir = Path(__file__).resolve().parent
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

# アプリ共通ロガーを初期化（GRP-12 ギャップ検出・受信エラー等の診断用）
from utils.logger import setup_logging, get_logger

_log_path = setup_logging()
_log = get_logger(__name__)
_log.info("CAN FD Log Analyzer 起動 — ログ出力先: %s", _log_path)

import flet as ft
from gui.main_window import MainWindow


def main(page: ft.Page):
    try:
        MainWindow(page)
    except Exception:
        _log.exception("MainWindow 初期化中に未捕捉例外")
        raise


if __name__ == "__main__":
    if hasattr(ft, "run"):
        ft.run(main)
    else:
        ft.app(target=main)
