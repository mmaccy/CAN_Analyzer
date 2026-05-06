"""接続設定ダイアログ — リアルタイム CAN 受信用

Vector / Virtual インターフェース選択、チャンネル、ボーレート（Arb/Data）、
CAN FD 有効化、ASC ログ保存先を入力させ、ReceiverConfig を返す。

Vector チャンネルは XL Driver から自動列挙する（未インストール時はマニュアル入力）。
ハードウェア未接続でも開発を進められるよう VirtualBus を選択肢に含める。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

import flet as ft

from realtime.can_receiver import ReceiverConfig, list_vector_channels


# 自動列挙できなかった場合のフォールバックチャンネル候補
_FALLBACK_CHANNEL_INDICES = [0, 1, 2, 3]


_INTERFACE_OPTIONS = [
    ("vector", "Vector (VN1610 等)"),
    ("virtual", "Virtual (テスト用)"),
]


class ConnectionDialog:
    """接続設定ダイアログ

    使用例:
        dlg = ConnectionDialog(page, on_submit=lambda cfg: ...)
        dlg.open()
    """

    def __init__(
        self,
        page: ft.Page,
        on_submit: Callable[[ReceiverConfig], None],
        initial: Optional[ReceiverConfig] = None,
    ) -> None:
        self._page = page
        self._on_submit = on_submit
        self._cfg = initial or ReceiverConfig()

        # ── 入力ウィジェット ────────────────────────────────
        # Flet 0.84+ では Dropdown のコールバックが on_select に改名されたため
        # 互換性確保のため hasattr で振り分ける（旧 API 環境でも動作させる）
        self._interface_dropdown = ft.Dropdown(
            label="インターフェース",
            value=self._cfg.interface,
            options=[ft.dropdown.Option(key=k, text=label) for k, label in _INTERFACE_OPTIONS],
            width=320,
        )
        if hasattr(self._interface_dropdown, "on_select"):
            self._interface_dropdown.on_select = self._on_interface_change
        else:
            self._interface_dropdown.on_change = self._on_interface_change

        # チャンネル選択は複数同時受信を許すため Checkbox 列で実装する。
        # Vector は同時に複数チャンネルを 1 つの Bus にまとめられる。
        # Virtual は単一のみ意味があるため UI で警告するに留める (検証は OK 時)。
        self._vector_channels = list_vector_channels()
        self._channel_checkboxes: List[ft.Checkbox] = []
        self._channel_column = ft.Column(
            controls=[],
            spacing=2,
            tight=True,
        )
        self._rebuild_channel_checkboxes(self._cfg.interface)

        self._app_name_field = ft.TextField(
            label="アプリ名 (Vector のみ)",
            value=self._cfg.app_name,
            width=320,
        )

        self._fd_checkbox = ft.Checkbox(
            label="CAN FD を有効にする",
            value=self._cfg.fd,
            on_change=self._on_fd_change,
        )

        self._bitrate_field = ft.TextField(
            label="Arbitration ボーレート (bps)",
            value=str(self._cfg.bitrate),
            width=200,
        )

        self._data_bitrate_field = ft.TextField(
            label="Data ボーレート (bps)",
            value=str(self._cfg.data_bitrate),
            width=200,
            disabled=not self._cfg.fd,
        )

        # ASC ログパス: ダイアログ起動時刻を yyyymmdd_hhmmss でデフォルト名に埋め込む
        self._default_log_basename = (
            f"rt_recording_{datetime.now().strftime('%Y%m%d_%H%M%S')}.asc"
        )
        self._log_path_field = ft.TextField(
            label="ASC ログ保存先 (空欄なら記録しない)",
            value=self._cfg.log_path or "",
            hint_text=self._default_log_basename,
            width=420,
        )
        self._log_picker = ft.FilePicker()
        # Flet 0.84+: FilePicker は services に登録（overlay は Dialog 等専用）
        # 0.28 系互換のため hasattr で分岐
        if hasattr(page, "services"):
            page.services.append(self._log_picker)
        else:
            page.overlay.append(self._log_picker)

        self._error_text = ft.Text("", color=ft.Colors.ERROR, size=11, visible=False)

        # ── ダイアログレイアウト ────────────────────────────
        # Vector チャンネル未検出時の案内
        ch_hint = self._channel_hint_text(self._cfg.interface)

        self._content_column = ft.Column(
            controls=[
                self._interface_dropdown,
                ft.Text("チャンネル選択 (複数可)", size=12, weight=ft.FontWeight.W_500),
                ft.Container(
                    content=self._channel_column,
                    padding=ft.padding.only(left=8),
                    border=ft.border.all(1, ft.Colors.OUTLINE_VARIANT),
                    border_radius=4,
                ),
                ch_hint,
                self._app_name_field,
                self._fd_checkbox,
                ft.Row([self._bitrate_field, self._data_bitrate_field], spacing=12),
                ft.Divider(),
                ft.Text("ASC ライブログ (推奨)", size=12, weight=ft.FontWeight.W_500),
                ft.Row(
                    [
                        self._log_path_field,
                        ft.IconButton(
                            ft.Icons.FOLDER_OPEN,
                            tooltip="保存先を選択",
                            on_click=self._on_browse_log,
                        ),
                    ],
                    spacing=4,
                ),
                self._error_text,
            ],
            tight=True,
            spacing=10,
            scroll=ft.ScrollMode.AUTO,
        )

        self._dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("CAN 接続設定"),
            content=ft.Container(content=self._content_column, width=480, height=620),
            actions=[
                ft.TextButton("キャンセル", on_click=self._on_cancel),
                ft.ElevatedButton("接続", icon=ft.Icons.LINK, on_click=self._on_ok),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )

        # interface 切替に応じて enable/disable を初期反映
        self._sync_enabled_state()

    # ---------- public ----------

    def open(self) -> None:
        """ダイアログを表示する

        Flet 0.84+: `page.show_dialog(dialog)` が正式 API。
        Flet 0.28 系: `page.dialog = dialog; dialog.open = True; page.update()`。
        """
        if hasattr(self._page, "show_dialog"):
            try:
                self._page.show_dialog(self._dialog)
                return
            except Exception:
                pass
        # Legacy path
        self._page.dialog = self._dialog
        self._dialog.open = True
        self._page.update()

    def close(self) -> None:
        if hasattr(self._page, "pop_dialog"):
            try:
                self._page.pop_dialog()
                return
            except Exception:
                pass
        # Legacy path
        self._dialog.open = False
        self._page.update()

    # ---------- internals ----------

    def _rebuild_channel_checkboxes(self, interface: str) -> None:
        """インターフェース種別に応じてチャンネル選択チェックボックスを再構築する"""
        # 既存設定で選択済みのチャンネル集合
        selected = set(self._cfg.channels or [])
        items: List = []
        if interface == "vector" and self._vector_channels:
            items = [(idx, f"Ch {idx}: {name}") for idx, name in self._vector_channels]
        else:
            items = [(idx, f"Ch {idx}") for idx in _FALLBACK_CHANNEL_INDICES]

        self._channel_checkboxes = []
        for idx, label in items:
            cb = ft.Checkbox(
                label=label,
                value=(idx in selected) or (not selected and idx == 0),
                on_change=self._on_channel_checkbox_change if interface == "virtual" else None,
                data=idx,
            )
            self._channel_checkboxes.append(cb)

        self._channel_column.controls = list(self._channel_checkboxes)

    def _on_channel_checkbox_change(self, e) -> None:
        """Virtual モード時: チェックボックスは単一選択を強制する (VirtualBus 制約)"""
        if self._interface_dropdown.value != "virtual":
            return
        clicked = e.control
        if not clicked.value:
            return
        for cb in self._channel_checkboxes:
            if cb is not clicked and cb.value:
                cb.value = False
                try:
                    cb.update()
                except Exception:
                    pass

    def _selected_channels(self) -> List[int]:
        """現在チェック中のチャンネル番号リスト (順序保持)"""
        return [cb.data for cb in self._channel_checkboxes if cb.value]

    def _channel_hint_text(self, interface: str) -> ft.Text:
        if interface == "vector" and not self._vector_channels:
            return ft.Text(
                "Vector XL Driver が検出できませんでした。0〜3 から接続したいチャンネルを選択してください (複数可)。",
                size=11,
                color=ft.Colors.ON_SURFACE_VARIANT,
                italic=True,
            )
        if interface == "virtual":
            return ft.Text(
                "VirtualBus はテスト用ループバック。単一チャンネルのみ有効です。",
                size=11,
                color=ft.Colors.ON_SURFACE_VARIANT,
                italic=True,
            )
        if interface == "vector":
            return ft.Text(
                "複数チャンネルを同時受信できます。受信したいチャンネルを全て選択してください。",
                size=11,
                color=ft.Colors.ON_SURFACE_VARIANT,
                italic=True,
            )
        return ft.Text("", size=1, visible=False)

    def _sync_enabled_state(self) -> None:
        is_vector = self._interface_dropdown.value == "vector"
        is_virtual = self._interface_dropdown.value == "virtual"
        self._app_name_field.disabled = not is_vector
        # VirtualBus は bitrate を解釈しないので無効化
        self._bitrate_field.disabled = is_virtual
        self._data_bitrate_field.disabled = is_virtual or not self._fd_checkbox.value

    def _on_interface_change(self, e) -> None:
        new_iface = self._interface_dropdown.value
        # チャンネルチェックボックスを再構築
        self._rebuild_channel_checkboxes(new_iface)
        # ヒント文言を差し替え (現在のレイアウト: hint は 4 番目 = index 3)
        new_hint = self._channel_hint_text(new_iface)
        controls = self._content_column.controls
        if len(controls) > 3:
            controls[3] = new_hint
        self._sync_enabled_state()
        try:
            self._content_column.update()
        except Exception:
            pass

    def _on_fd_change(self, e) -> None:
        self._sync_enabled_state()
        try:
            self._data_bitrate_field.update()
        except Exception:
            pass

    def _on_browse_log(self, e) -> None:
        # ダイアログ起動時刻ベースのデフォルト名 (yyyymmdd_hhmmss 入り)
        default_name = self._default_log_basename
        # Flet 0.84 では save_file が awaitable のため、Legacy/New 双方に対応
        if hasattr(ft, "FilePickerResultEvent"):
            # Legacy: コールバックで結果取得
            self._log_picker.on_result = self._on_log_picked_legacy
            self._log_picker.save_file(
                dialog_title="ASC ログ保存先",
                allowed_extensions=["asc"],
                file_type=ft.FilePickerFileType.CUSTOM,
                file_name=default_name,
            )
        else:
            async def _pick():
                result = await self._log_picker.save_file(
                    dialog_title="ASC ログ保存先",
                    allowed_extensions=["asc"],
                    file_type=ft.FilePickerFileType.CUSTOM,
                    file_name=default_name,
                )
                if result:
                    path = result if isinstance(result, str) else result.path
                    self._log_path_field.value = path
                    self._log_path_field.update()

            try:
                self._page.run_task(_pick)
            except Exception:
                pass

    def _on_log_picked_legacy(self, e) -> None:
        if not getattr(e, "path", None):
            return
        path = e.path
        if not path.lower().endswith(".asc"):
            path += ".asc"
        self._log_path_field.value = path
        try:
            self._log_path_field.update()
        except Exception:
            pass

    def _on_cancel(self, e) -> None:
        self.close()

    def _on_ok(self, e) -> None:
        # 入力値検証
        channels = self._selected_channels()
        if not channels:
            self._show_error("チャンネルを 1 つ以上選択してください")
            return
        # Virtual は単一のみ意味があるので警告（複数チェックでも先頭を採用）
        interface = self._interface_dropdown.value or "virtual"
        if interface == "virtual" and len(channels) > 1:
            self._show_error("Virtual は単一チャンネルのみ対応です。1 つに絞ってください")
            return

        bitrate = self._cfg.bitrate
        data_bitrate = self._cfg.data_bitrate
        try:
            if not self._bitrate_field.disabled:
                bitrate = int(self._bitrate_field.value)
            if not self._data_bitrate_field.disabled:
                data_bitrate = int(self._data_bitrate_field.value)
        except ValueError:
            self._show_error("ボーレートは整数で指定してください")
            return

        log_path = (self._log_path_field.value or "").strip() or None
        if log_path and not log_path.lower().endswith(".asc"):
            log_path += ".asc"

        cfg = ReceiverConfig(
            interface=interface,
            channels=channels,
            app_name=(self._app_name_field.value or "CAN_FD_Analyzer").strip(),
            fd=bool(self._fd_checkbox.value),
            bitrate=bitrate,
            data_bitrate=data_bitrate,
            log_path=log_path,
        )
        self.close()
        self._on_submit(cfg)

    def _show_error(self, msg: str) -> None:
        self._error_text.value = msg
        self._error_text.visible = True
        try:
            self._error_text.update()
        except Exception:
            pass
