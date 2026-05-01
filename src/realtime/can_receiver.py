"""リアルタイム CAN 受信ワーカー

python-can の Bus を専用スレッドで監視し、受信メッセージを CanFrame に
変換して thread-safe queue に投入する。GUI スレッドは drain() で
バッチ取得する。

接続先:
- Vector XL Driver (VN1610 等) — interface="vector"
- VirtualBus — interface="virtual" (ハードウェア無しでテスト用)

ASC ライブログ:
- ReceiverConfig.log_path が指定されていれば、受信フレームを逐次書き出す。
- 受信スレッドから書き出すため、ファイル I/O は受信レートを律速する可能性あり。
  GB 級の長時間記録では別途バッファリング検討。
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from models.can_frame import DLC_TO_LENGTH, CanFrame
from can_parser.asc_writer import format_frame_as_asc, write_default_header
from utils.logger import get_logger


_log = get_logger(__name__)


# CAN FD: data_length → DLC code 逆引き
_LENGTH_TO_DLC = {v: k for k, v in DLC_TO_LENGTH.items()}


@dataclass
class ReceiverConfig:
    """python-can 接続設定

    複数チャンネル同時受信 (Vector) に対応する。`channels` は受信したい
    物理チャンネル番号のリスト。VirtualBus の場合は 1 件のみが意味を持つ
    （複数指定された場合は先頭のみ使用）。
    """

    interface: str = "virtual"  # "vector" | "virtual" | その他 python-can 対応
    channels: List = field(default_factory=lambda: [0])  # List[Union[int, str]]
    app_name: str = "CAN_FD_Analyzer"  # Vector のみ意味あり
    fd: bool = True
    bitrate: int = 500_000
    data_bitrate: int = 2_000_000
    log_path: Optional[str] = None  # None なら ASC 記録なし

    def to_bus_kwargs(self) -> dict:
        """python-can.Bus に渡す kwargs を組み立てる

        Vector で 2ch 以上指定された場合は `channel=[0, 1, ...]` のリスト形式で
        渡し、python-can VectorBus の複数チャンネル同時受信機能を利用する。
        """
        if not self.channels:
            ch_param = 0
        elif self.interface == "virtual":
            # VirtualBus は単一チャンネルのみ対応
            ch_param = self.channels[0]
        elif len(self.channels) == 1:
            ch_param = self.channels[0]
        else:
            ch_param = list(self.channels)

        kwargs = {
            "interface": self.interface,
            "channel": ch_param,
        }
        if self.interface == "vector":
            kwargs["app_name"] = self.app_name
            kwargs["fd"] = self.fd
            kwargs["bitrate"] = self.bitrate
            if self.fd:
                kwargs["data_bitrate"] = self.data_bitrate
        elif self.interface == "virtual":
            # VirtualBus は bitrate を解釈しない
            pass
        else:
            kwargs["bitrate"] = self.bitrate
            if self.fd:
                kwargs["fd"] = True
                kwargs["data_bitrate"] = self.data_bitrate
        return kwargs


@dataclass
class ReceiverStats:
    """受信統計（GUI 表示用スナップショット）"""

    rx_count: int = 0
    error_count: int = 0
    dropped_count: int = 0
    started_at: float = 0.0
    last_rx_at: float = 0.0


def list_vector_channels() -> List[Tuple[int, str]]:
    """利用可能な Vector チャンネルを列挙する。XL Driver 未インストール時は空。"""
    try:
        from can.interfaces.vector import canlib  # type: ignore
        configs = canlib.get_channel_configs()
        result: List[Tuple[int, str]] = []
        for cfg in configs:
            ch_idx = getattr(cfg, "channel_index", None)
            name = getattr(cfg, "name", None) or getattr(cfg, "transceiver_name", "")
            if ch_idx is not None:
                result.append((int(ch_idx), str(name)))
        return result
    except Exception as ex:
        # Vector XL Driver 未インストール時の `Could not import vxlapi` は想定内
        _log.debug("Vector チャンネル列挙不可: %s", ex)
        return []


class CanReceiver:
    """python-can Bus を非同期で監視するワーカー

    使用例:
        cfg = ReceiverConfig(interface="virtual", channel=0)
        rx = CanReceiver(cfg)
        rx.start()
        while running:
            frames = rx.drain()
            ...
        rx.stop()
    """

    # 受信キューが満杯になった場合のドロップ閾値（メモリ保護）
    _QUEUE_MAX = 50_000

    def __init__(self, config: ReceiverConfig) -> None:
        self._config = config
        self._bus = None  # type: ignore[var-annotated]
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._queue: "queue.Queue[CanFrame]" = queue.Queue(maxsize=self._QUEUE_MAX)
        self._stats = ReceiverStats()
        self._stats_lock = threading.Lock()
        self._t0_monotonic: Optional[float] = None
        self._log_file = None  # type: ignore[var-annotated]
        self._log_lock = threading.Lock()

    # ---------- lifecycle ----------

    def start(self) -> None:
        """Bus を開いて受信スレッドを起動する。失敗時は例外を伝播。"""
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("受信は既に開始されています")

        import can  # 遅延 import: python-can 未導入環境でも GUI 起動だけは通したい

        bus_kwargs = self._config.to_bus_kwargs()
        self._bus = can.Bus(**bus_kwargs)

        # ASC ログオープン
        if self._config.log_path:
            self._log_file = open(self._config.log_path, "w", encoding="utf-8", newline="\n")
            write_default_header(self._log_file, fd=self._config.fd)

        self._t0_monotonic = time.monotonic()
        self._stop_event.clear()
        with self._stats_lock:
            self._stats = ReceiverStats(started_at=time.time())

        self._thread = threading.Thread(target=self._receive_loop, daemon=True, name="CanReceiver")
        self._thread.start()

    def stop(self) -> None:
        """受信スレッドを停止して Bus / ログを閉じる"""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            if self._thread.is_alive():
                _log.warning("受信スレッドが 2 秒以内に停止しませんでした (デーモンスレッドのため放置)")
            self._thread = None
        if self._bus is not None:
            try:
                self._bus.shutdown()
            except Exception:
                _log.exception("bus.shutdown() で例外")
            self._bus = None
        with self._log_lock:
            if self._log_file is not None:
                try:
                    self._log_file.flush()
                    self._log_file.close()
                except Exception:
                    _log.exception("ASC ログクローズで例外")
                self._log_file = None
        stats = self.get_stats()
        _log.info(
            "受信停止: rx=%d drop=%d err=%d", stats.rx_count, stats.dropped_count, stats.error_count,
        )

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ---------- data access ----------

    def drain(self, max_items: int = 2000) -> List[CanFrame]:
        """キューから最大 max_items 件のフレームを取り出す（GUI スレッドから呼ぶ）"""
        out: List[CanFrame] = []
        for _ in range(max_items):
            try:
                out.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return out

    def get_stats(self) -> ReceiverStats:
        """統計のスナップショットを返す"""
        with self._stats_lock:
            return ReceiverStats(
                rx_count=self._stats.rx_count,
                error_count=self._stats.error_count,
                dropped_count=self._stats.dropped_count,
                started_at=self._stats.started_at,
                last_rx_at=self._stats.last_rx_at,
            )

    # ---------- worker thread ----------

    def _receive_loop(self) -> None:
        """受信スレッド本体: bus.recv() をループして CanFrame に変換"""
        bus = self._bus
        if bus is None:
            _log.error("受信ループ開始時に bus が None です")
            return
        _log.info(
            "受信ループ開始 (interface=%s, channels=%s)",
            self._config.interface, self._config.channels,
        )
        while not self._stop_event.is_set():
            try:
                msg = bus.recv(timeout=0.1)
            except Exception:
                _log.exception("bus.recv() で例外 (interface=%s)", self._config.interface)
                with self._stats_lock:
                    self._stats.error_count += 1
                continue
            if msg is None:
                continue
            try:
                frame = self._convert(msg)
            except Exception:
                _log.exception("メッセージ変換で例外 (id=0x%X)", getattr(msg, "arbitration_id", 0))
                with self._stats_lock:
                    self._stats.error_count += 1
                continue

            # キュー投入（満杯ならドロップ）
            try:
                self._queue.put_nowait(frame)
            except queue.Full:
                with self._stats_lock:
                    if self._stats.dropped_count == 0:
                        _log.warning(
                            "受信キューが満杯になりフレームをドロップ (キュー上限=%d)",
                            self._QUEUE_MAX,
                        )
                    self._stats.dropped_count += 1

            # ASC ログ書込
            if self._log_file is not None:
                try:
                    line = format_frame_as_asc(frame)
                    with self._log_lock:
                        if self._log_file is not None:
                            self._log_file.write(line + "\n")
                except Exception:
                    _log.exception("ASC ログ書込で例外")
                    with self._stats_lock:
                        self._stats.error_count += 1

            with self._stats_lock:
                self._stats.rx_count += 1
                self._stats.last_rx_at = time.time()

    def _convert(self, msg) -> CanFrame:
        """python-can の Message を CanFrame に変換

        タイムスタンプは「受信開始からの経過秒」に正規化する（ASC の慣例）。
        msg.timestamp は OS 時刻（epoch）の場合があるため、0 始まりに変換。
        """
        # 経過秒に正規化
        if self._t0_monotonic is None:
            ts = 0.0
        else:
            # python-can の timestamp は epoch 秒。受信時刻 - 起動時刻 を相対化したい。
            # ただし VirtualBus 等で 0 開始の場合もあるので、msg.timestamp が大きい (epoch)
            # なら time.time() - started_at で代替する。
            if msg.timestamp and msg.timestamp > 1e9:
                ts = time.time() - self._stats.started_at
            else:
                ts = float(msg.timestamp or 0.0)

        is_fd = bool(getattr(msg, "is_fd", False))
        data = bytes(msg.data) if msg.data is not None else b""
        data_length = len(data)

        if is_fd:
            dlc = _LENGTH_TO_DLC.get(data_length, msg.dlc or 0)
        else:
            dlc = msg.dlc if msg.dlc is not None else data_length

        # channel: python-can は str / int / None を返しうる。CanFrame は int を要求するので
        # ASC フォーマット失敗を避けるため必ず int に正規化する（VirtualBus 等で str の場合あり）。
        # 複数チャンネル同時受信時は msg.channel が物理チャンネル番号 (int) を持つ。
        ch_raw = getattr(msg, "channel", None)
        if isinstance(ch_raw, int):
            channel = ch_raw
        elif isinstance(ch_raw, str) and ch_raw.isdigit():
            channel = int(ch_raw)
        elif self._config.channels and isinstance(self._config.channels[0], int):
            channel = self._config.channels[0]
        else:
            channel = 0

        # is_rx: python-can 4.0+ では Message.is_rx が利用可能。未設定なら True。
        is_rx = bool(getattr(msg, "is_rx", True))

        return CanFrame(
            timestamp=ts,
            channel=channel,
            arbitration_id=int(msg.arbitration_id),
            is_extended_id=bool(msg.is_extended_id),
            is_fd=is_fd,
            is_rx=is_rx,
            dlc=dlc,
            data_length=data_length,
            data=data,
            frame_name=None,
            brs=bool(getattr(msg, "bitrate_switch", False)) if is_fd else None,
            esi=bool(getattr(msg, "error_state_indicator", False)) if is_fd else None,
            raw_line="",
            file_offset=0,
        )
