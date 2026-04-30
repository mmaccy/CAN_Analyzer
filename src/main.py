"""CAN FD Log Analyzer — Flet エントリポイント"""

import logging
import sys
from pathlib import Path

# コンソールにログ出力（GRP-12 ギャップ検出等の診断用）
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)

# src ディレクトリをパスに追加
src_dir = Path(__file__).resolve().parent
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

import flet as ft
from gui.main_window import MainWindow


def main(page: ft.Page):
    MainWindow(page)


if __name__ == "__main__":
    if hasattr(ft, "run"):
        ft.run(main)
    else:
        ft.app(target=main)
