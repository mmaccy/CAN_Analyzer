"""asc_writer.format_frame_as_asc のテスト

リアルタイム受信ログ用のフレームフォーマッタが、
asc_parser でラウンドトリップ可能な行を生成することを確認する。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from models.can_frame import CanFrame
from can_parser.asc_writer import format_frame_as_asc, write_default_header
from can_parser.asc_parser import _parse_classic_line, _parse_canfd_line, iter_frames


def test_classic_can_roundtrip():
    """Classic CAN フレームのフォーマット → パースで値が一致する"""
    f = CanFrame(
        timestamp=0.012345,
        channel=1,
        arbitration_id=0x1AB,
        is_extended_id=False,
        is_fd=False,
        is_rx=True,
        dlc=8,
        data_length=8,
        data=bytes(range(8)),
    )
    line = format_frame_as_asc(f)
    parsed = _parse_classic_line(line + "\n", 0)
    assert parsed is not None
    assert parsed.timestamp == 0.012345
    assert parsed.channel == 1
    assert parsed.arbitration_id == 0x1AB
    assert parsed.is_extended_id is False
    assert parsed.is_fd is False
    assert parsed.is_rx is True
    assert parsed.dlc == 8
    assert parsed.data == bytes(range(8))


def test_extended_id_roundtrip():
    """拡張 ID フレーム (id 末尾 x) も正しくフォーマット/パースできる"""
    f = CanFrame(
        timestamp=1.0,
        channel=2,
        arbitration_id=0xAFACE31,
        is_extended_id=True,
        is_fd=False,
        is_rx=False,
        dlc=4,
        data_length=4,
        data=b"\xDE\xAD\xBE\xEF",
    )
    line = format_frame_as_asc(f)
    assert "AFACE31x" in line, f"拡張 ID マーカーが欠落: {line!r}"
    parsed = _parse_classic_line(line + "\n", 0)
    assert parsed is not None
    assert parsed.is_extended_id is True
    assert parsed.is_rx is False
    assert parsed.data == b"\xDE\xAD\xBE\xEF"


def test_canfd_roundtrip_8byte():
    """CAN FD 8 バイトフレームのラウンドトリップ"""
    f = CanFrame(
        timestamp=0.5,
        channel=2,
        arbitration_id=0x29C,
        is_extended_id=False,
        is_fd=True,
        is_rx=True,
        dlc=8,
        data_length=8,
        data=b"\x00\x07\xDC\xA9\x48\xAA\x6C\x20",
        frame_name="VSA_29C",
        brs=True,
        esi=False,
    )
    line = format_frame_as_asc(f)
    parsed = _parse_canfd_line(line + "\n", 0)
    assert parsed is not None
    assert parsed.is_fd is True
    assert parsed.arbitration_id == 0x29C
    assert parsed.frame_name == "VSA_29C"
    assert parsed.brs is True
    assert parsed.esi is False
    assert parsed.dlc == 8
    assert parsed.data_length == 8
    assert parsed.data == b"\x00\x07\xDC\xA9\x48\xAA\x6C\x20"


def test_canfd_roundtrip_64byte():
    """CAN FD 64 バイト (DLC=0xF) フレームのラウンドトリップ"""
    payload = bytes(i % 256 for i in range(64))
    f = CanFrame(
        timestamp=2.0,
        channel=1,
        arbitration_id=0xCD9AD5C,
        is_extended_id=True,
        is_fd=True,
        is_rx=True,
        dlc=15,
        data_length=64,
        data=payload,
        frame_name=None,  # DBC 未定義想定
        brs=True,
        esi=True,
    )
    line = format_frame_as_asc(f)
    parsed = _parse_canfd_line(line + "\n", 0)
    assert parsed is not None
    assert parsed.is_extended_id is True
    assert parsed.dlc == 15
    assert parsed.data_length == 64
    assert parsed.data == payload
    assert parsed.brs is True
    assert parsed.esi is True


def test_full_file_roundtrip(tmp_path):
    """ヘッダ + 複数フレームを書き出し → iter_frames で読み戻せる"""
    f1 = CanFrame(
        timestamp=0.001, channel=1, arbitration_id=0x100,
        is_extended_id=False, is_fd=False, is_rx=True,
        dlc=4, data_length=4, data=b"\x01\x02\x03\x04",
    )
    f2 = CanFrame(
        timestamp=0.002, channel=2, arbitration_id=0x200,
        is_extended_id=False, is_fd=True, is_rx=True,
        dlc=8, data_length=8, data=b"\x10" * 8,
        frame_name="TestFrame", brs=True, esi=False,
    )
    output = tmp_path / "rt.asc"
    with open(output, "w", encoding="utf-8") as fh:
        write_default_header(fh, fd=True)
        fh.write(format_frame_as_asc(f1) + "\n")
        fh.write(format_frame_as_asc(f2) + "\n")

    frames = list(iter_frames(str(output)))
    assert len(frames) == 2
    assert frames[0].arbitration_id == 0x100
    assert frames[0].is_fd is False
    assert frames[1].arbitration_id == 0x200
    assert frames[1].is_fd is True
    assert frames[1].frame_name == "TestFrame"


if __name__ == "__main__":
    import tempfile
    test_classic_can_roundtrip()
    test_extended_id_roundtrip()
    test_canfd_roundtrip_8byte()
    test_canfd_roundtrip_64byte()
    with tempfile.TemporaryDirectory() as d:
        test_full_file_roundtrip(Path(d))
    print("=== ALL ASC WRITER TESTS PASSED ===")
