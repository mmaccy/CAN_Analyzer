"""ASC パーサの基本テスト"""
import sys
from pathlib import Path

# src をパスに追加
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from can_parser.asc_parser import parse_header, load_all_frames, _parse_canfd_line, _parse_classic_line

# テスト用 ASC データ
SAMPLE_ASC = """date Fri Apr 17 10:43:18.549 am 2026
base hex
timestamps absolute
internal events logged
// version 17.6.0
// Measurement UUID: 9efa2bed-acf1-48b8-89a2-e6eb0da19f9c
Begin TriggerBlock Fri Apr 17 10:43:18.549 am 2026
   0.000000 Start of measurement
   0.004603 CANFD   2 Rx        29c  VSA_29C                          1 0 8  8 00 07 dc a9 48 aa 6c 20   100704  130   303000 b000809a 46500250 4b280150 20011736 2000091c
   0.005454 1  AFACE31x        Rx   d 8 4C B0 00 00 00 00 00 00  Length = 1103547 BitCount = 142 ID = 184208945x
   0.018189 CANFD   2 Rx    cd9ad5cx BDCAN1_0CD9AD5C                  1 0 f 64 00 00 40 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 21 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00   413203  698   303000 b0117866 46500250 4b280150 20011736 2000091c
   0.013988 CANFD   3 Rx        45a  BDCAN11_45A                      1 0 2  2 20 03    75703   80   303000 9800144d 46500250 4b280150 20011736 2000091c
"""


def test_parse():
    # テストファイル作成
    test_file = Path(__file__).parent / "_test_sample.asc"
    test_file.write_text(SAMPLE_ASC, encoding="utf-8")

    try:
        # ヘッダテスト
        header = parse_header(str(test_file))
        assert header.base == "hex", f"Expected 'hex', got '{header.base}'"
        assert header.timestamps == "absolute"
        assert header.version == "17.6.0"
        assert "9efa2bed" in header.measurement_uuid
        print(f"[OK] Header: date={header.date}, version={header.version}")

        # フレーム読込テスト
        frames = load_all_frames(str(test_file))
        print(f"[OK] Loaded {len(frames)} frames")
        assert len(frames) == 4, f"Expected 4 frames, got {len(frames)}"

        # CAN FD 8byte
        f0 = frames[0]
        assert f0.is_fd == True
        assert f0.channel == 2
        assert f0.arbitration_id == 0x29c
        assert f0.is_extended_id == False
        assert f0.frame_name == "VSA_29C"
        assert f0.dlc == 8
        assert f0.data_length == 8
        assert f0.brs == True
        assert f0.esi == False
        print(f"[OK] CAN FD 8B: {f0.id_hex} {f0.frame_name} data={f0.data_hex}")

        # Classic CAN extended ID
        f1 = frames[1]
        assert f1.is_fd == False
        assert f1.channel == 1
        assert f1.arbitration_id == 0xAFACE31
        assert f1.is_extended_id == True
        assert f1.dlc == 8
        print(f"[OK] Classic CAN ext: {f1.id_hex} data={f1.data_hex}")

        # CAN FD 64byte
        f2 = frames[2]
        assert f2.is_fd == True
        assert f2.arbitration_id == 0xcd9ad5c
        assert f2.is_extended_id == True
        assert f2.dlc == 15  # 0xf
        assert f2.data_length == 64
        assert len(f2.data) == 64
        assert f2.frame_name == "BDCAN1_0CD9AD5C"
        print(f"[OK] CAN FD 64B: {f2.id_hex} {f2.frame_name} len={len(f2.data)}")

        # CAN FD 2byte
        f3 = frames[3]
        assert f3.data_length == 2
        assert f3.frame_name == "BDCAN11_45A"
        print(f"[OK] CAN FD 2B: {f3.id_hex} {f3.frame_name} data={f3.data_hex}")

        print("\n=== ALL TESTS PASSED ===")

    finally:
        test_file.unlink(missing_ok=True)


if __name__ == "__main__":
    test_parse()
