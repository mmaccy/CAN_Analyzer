"""CanReceiver の VirtualBus を使ったエンドツーエンドテスト

ハードウェアなしで動作可能。python-can の VirtualBus を相手にして
送受信・キュードレイン・ASC ライブログの書込を検証する。
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import can  # type: ignore

from realtime.can_receiver import CanReceiver, ReceiverConfig
from can_parser.asc_parser import iter_frames


def _drain_until(rx: CanReceiver, expected: int, timeout: float = 1.0):
    """指定件数受信されるまで待機して drain して返す"""
    deadline = time.monotonic() + timeout
    collected = []
    while time.monotonic() < deadline:
        collected.extend(rx.drain())
        if len(collected) >= expected:
            break
        time.sleep(0.05)
    return collected


def test_classic_send_recv():
    """Classic CAN: 送信したフレームをそのまま受信できる"""
    cfg = ReceiverConfig(interface="virtual", channels=["canX_classic"], fd=False)
    rx = CanReceiver(cfg)
    rx.start()
    tx = can.Bus(interface="virtual", channel="canX_classic")
    try:
        tx.send(can.Message(arbitration_id=0x123, data=b"\x01\x02\x03\x04", is_extended_id=False))
        tx.send(can.Message(arbitration_id=0x456, data=b"\x10\x20", is_extended_id=False))
        frames = _drain_until(rx, 2)
    finally:
        tx.shutdown()
        rx.stop()

    assert len(frames) >= 2
    ids = {f.arbitration_id for f in frames}
    assert 0x123 in ids
    assert 0x456 in ids
    f0 = next(f for f in frames if f.arbitration_id == 0x123)
    assert f0.data == b"\x01\x02\x03\x04"
    assert f0.is_fd is False
    # チャンネルは int に正規化される
    assert isinstance(f0.channel, int)


def test_canfd_send_recv():
    """CAN FD 64 バイトフレームの送受信"""
    cfg = ReceiverConfig(interface="virtual", channels=["canX_fd"], fd=True)
    rx = CanReceiver(cfg)
    rx.start()
    tx = can.Bus(interface="virtual", channel="canX_fd")
    try:
        payload = bytes(range(64))
        tx.send(can.Message(
            arbitration_id=0x789,
            data=payload,
            is_fd=True,
            bitrate_switch=True,
            is_extended_id=False,
        ))
        frames = _drain_until(rx, 1)
    finally:
        tx.shutdown()
        rx.stop()

    assert len(frames) >= 1
    f = frames[0]
    assert f.is_fd is True
    assert f.data == bytes(range(64))
    assert f.data_length == 64
    assert f.dlc == 15  # 64 bytes → DLC code 0xF
    assert f.brs is True


def test_log_path_writes_asc(tmp_path):
    """log_path 指定時、受信フレームが ASC ファイルに書き出され parser で読み戻せる"""
    log_file = tmp_path / "rt_log.asc"
    cfg = ReceiverConfig(
        interface="virtual",
        channels=["canX_log"],
        fd=False,
        log_path=str(log_file),
    )
    rx = CanReceiver(cfg)
    rx.start()
    tx = can.Bus(interface="virtual", channel="canX_log")
    try:
        for i in range(3):
            tx.send(can.Message(arbitration_id=0x100 + i, data=bytes([i, i, i, i]), is_extended_id=False))
        _drain_until(rx, 3)
    finally:
        tx.shutdown()
        rx.stop()

    assert log_file.exists()
    parsed = list(iter_frames(str(log_file)))
    assert len(parsed) == 3
    assert {f.arbitration_id for f in parsed} == {0x100, 0x101, 0x102}


def test_stats_count_increments():
    """rx_count が受信件数に応じて増える"""
    cfg = ReceiverConfig(interface="virtual", channels=["canX_stats"], fd=False)
    rx = CanReceiver(cfg)
    rx.start()
    tx = can.Bus(interface="virtual", channel="canX_stats")
    try:
        for i in range(5):
            tx.send(can.Message(arbitration_id=0x010 + i, data=b"\x00", is_extended_id=False))
        _drain_until(rx, 5)
        stats = rx.get_stats()
    finally:
        tx.shutdown()
        rx.stop()

    assert stats.rx_count >= 5
    assert stats.error_count == 0


def test_double_start_raises():
    """既に起動中の Receiver は再 start で例外"""
    cfg = ReceiverConfig(interface="virtual", channels=["canX_double"], fd=False)
    rx = CanReceiver(cfg)
    rx.start()
    try:
        with pytest.raises(RuntimeError):
            rx.start()
    finally:
        rx.stop()


def test_receiver_config_to_bus_kwargs_vector():
    """Vector 設定の bus_kwargs に app_name/fd/data_bitrate が含まれる"""
    cfg = ReceiverConfig(
        interface="vector", channels=[1], app_name="X",
        fd=True, bitrate=500000, data_bitrate=2000000,
    )
    kw = cfg.to_bus_kwargs()
    assert kw["interface"] == "vector"
    assert kw["channel"] == 1
    assert kw["app_name"] == "X"
    assert kw["fd"] is True
    assert kw["bitrate"] == 500000
    assert kw["data_bitrate"] == 2000000


def test_receiver_config_to_bus_kwargs_virtual_no_bitrate():
    """Virtual インターフェースは bitrate を含まない (VirtualBus が解釈しないため)"""
    cfg = ReceiverConfig(interface="virtual", channels=[0], fd=False)
    kw = cfg.to_bus_kwargs()
    assert kw["interface"] == "virtual"
    assert "bitrate" not in kw
    assert "data_bitrate" not in kw


def test_receiver_config_multi_channel_vector():
    """Vector で複数チャンネル指定時は channel=list で渡る"""
    cfg = ReceiverConfig(
        interface="vector", channels=[0, 1, 2], app_name="X",
        fd=True, bitrate=500000, data_bitrate=2000000,
    )
    kw = cfg.to_bus_kwargs()
    assert kw["channel"] == [0, 1, 2]


def test_receiver_config_single_channel_scalar():
    """単一チャンネルの場合は channel=スカラ値で渡る (Vector 1ch も list 化しない)"""
    cfg = ReceiverConfig(interface="vector", channels=[2], fd=True)
    kw = cfg.to_bus_kwargs()
    assert kw["channel"] == 2


def test_receiver_config_virtual_takes_first_channel_only():
    """VirtualBus は単一チャンネル前提なので、複数指定時は先頭のみ採用される"""
    cfg = ReceiverConfig(interface="virtual", channels=["a", "b", "c"])
    kw = cfg.to_bus_kwargs()
    assert kw["channel"] == "a"
