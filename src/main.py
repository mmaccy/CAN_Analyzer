"""CAN FD Log Analyzer — Flet エントリポイント"""

import sys
from pathlib import Path

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
