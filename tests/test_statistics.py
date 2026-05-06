"""統計計算と CSV エクスポートのテスト"""
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from models.can_frame import CanFrame
from analysis.statistics import compute_frame_statistics, compute_bus_load
from gui.statistics_panel import write_statistics_csv


def _mk_frame(ts: float, arb_id: int = 0x100, ch: int = 0, name: str = None) -> CanFrame:
    return CanFrame(
        timestamp=ts, channel=ch, arbitration_id=arb_id,
        is_extended_id=False, is_fd=False, is_rx=True,
        dlc=1, data_length=1, data=b"\x00",
        frame_name=name,
    )


def test_frame_statistics_periodic():
    """10ms 周期のフレームから周期統計が得られる"""
    frames = [_mk_frame(i * 0.01, name="PERIODIC_100") for i in range(11)]
    stats = compute_frame_statistics(frames)
    assert len(stats) == 1
    s = stats[0]
    assert s.arbitration_id == 0x100
    assert s.frame_name == "PERIODIC_100"
    assert s.count == 11
    # avg ≈ 10ms
    assert abs(s.cycle_avg_ms - 10.0) < 0.01
    assert abs(s.cycle_min_ms - 10.0) < 0.01
    assert abs(s.cycle_max_ms - 10.0) < 0.01
    # 完全周期なので std ≈ 0
    assert s.cycle_std_ms < 1e-6


def test_frame_statistics_multiple_ids():
    """複数 ID は (id, channel) ごとに集約される"""
    frames = []
    for i in range(5):
        frames.append(_mk_frame(i * 0.02, arb_id=0x100))
    for i in range(3):
        frames.append(_mk_frame(i * 0.05, arb_id=0x200))
    stats = compute_frame_statistics(frames)
    assert len(stats) == 2
    by_id = {s.arbitration_id: s for s in stats}
    assert by_id[0x100].count == 5
    assert by_id[0x200].count == 3


def test_frame_statistics_single_frame_zero_cycle():
    """フレームが 1 件のみだと周期 = 0"""
    frames = [_mk_frame(1.0)]
    stats = compute_frame_statistics(frames)
    assert len(stats) == 1
    assert stats[0].count == 1
    assert stats[0].cycle_avg_ms == 0.0


def test_bus_load_basic():
    """100 frames/sec × Classic 8byte で バスロード ≈ 2.72% (500kbps 想定)"""
    # ヘッダ72bit + データ8byte=64bit = 136bit/frame
    # 100 frames/sec × 136bit / 500_000bps = 2.72%
    frames = []
    for i in range(100):
        frames.append(CanFrame(
            timestamp=i * 0.01, channel=0, arbitration_id=0x100,
            is_extended_id=False, is_fd=False, is_rx=True,
            dlc=8, data_length=8, data=b"\x00" * 8,
        ))
    load = compute_bus_load(frames, interval_sec=1.0)
    assert 0 in load
    first_t, first_pct = load[0][0]
    assert 2.0 < first_pct < 3.5


def test_csv_export_roundtrip(tmp_path):
    """write_statistics_csv が CSV 行を正しく書き出す"""
    frames = [_mk_frame(i * 0.01, arb_id=0x100, name="MSG_100") for i in range(5)]
    frames += [_mk_frame(i * 0.05, arb_id=0x200, ch=1) for i in range(3)]
    stats = compute_frame_statistics(frames)

    output = tmp_path / "stats.csv"
    n = write_statistics_csv(stats, str(output))
    assert n == 2

    with open(output, encoding="utf-8") as f:
        rows = list(csv.reader(f))
    assert rows[0] == [
        "ID(hex)", "Name", "Channel", "Count",
        "AvgCycle(ms)", "MinCycle(ms)", "MaxCycle(ms)", "StdDev(ms)",
        "FirstTime(s)", "LastTime(s)",
    ]
    assert len(rows) == 3  # header + 2 data
    # 0x100 行
    row_100 = next(r for r in rows[1:] if r[0] == "0x100")
    assert row_100[1] == "MSG_100"
    assert row_100[2] == "0"  # channel
    assert row_100[3] == "5"  # count
    # 0x200 行 (frame_name=None → "")
    row_200 = next(r for r in rows[1:] if r[0] == "0x200")
    assert row_200[1] == ""
    assert row_200[2] == "1"
    assert row_200[3] == "3"
