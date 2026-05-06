"""DbcLoader のテスト

特に Value Table (VAL_) デコードとフレーム名解決を検証する。
DBC は cantools が in-memory 文字列ロード (load_string) を提供するため、
テスト用の最小 DBC を文字列で組み立ててテストする。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from models.can_frame import CanFrame
from can_parser.dbc_loader import DbcLoader


# 最小 DBC: 1 メッセージ + 2 シグナル (うち 1 つに VAL_)
SAMPLE_DBC = """VERSION ""

NS_ :

BS_:

BU_:

BO_ 256 EngineState: 8 Vector__XXX
 SG_ State : 0|8@1+ (1,0) [0|255] "" Vector__XXX
 SG_ Speed : 8|16@1+ (0.1,0) [0|6553.5] "rpm" Vector__XXX

VAL_ 256 State 0 "Off" 1 "Idle" 2 "Running" 3 "Error" ;
"""


def _make_loader(tmp_path):
    dbc_file = tmp_path / "test.dbc"
    dbc_file.write_text(SAMPLE_DBC, encoding="utf-8")
    loader = DbcLoader()
    loader.load_file(str(dbc_file))
    return loader


def test_frame_name_resolution(tmp_path):
    """get_frame_name が VAL_/BO_ で定義した名前を返す"""
    loader = _make_loader(tmp_path)
    assert loader.get_frame_name(256) == "EngineState"
    # 未定義
    assert loader.get_frame_name(0xDEAD) is None


def test_resolve_frame_names_batch(tmp_path):
    """フレームリスト一括解決"""
    loader = _make_loader(tmp_path)
    frames = [
        CanFrame(timestamp=0, channel=0, arbitration_id=256,
                 is_extended_id=False, is_fd=False, is_rx=True,
                 dlc=3, data_length=3, data=b"\x02\x10\x27"),
        CanFrame(timestamp=0, channel=0, arbitration_id=999,
                 is_extended_id=False, is_fd=False, is_rx=True,
                 dlc=1, data_length=1, data=b"\x00"),
    ]
    loader.resolve_frame_names(frames)
    assert frames[0].frame_name == "EngineState"
    assert frames[1].frame_name is None


def test_decode_frame_basic(tmp_path):
    """シグナル値の物理値・raw 値が正しく算出される"""
    loader = _make_loader(tmp_path)
    # State=2 (Running), Speed=10000 raw → physical=1000.0 rpm
    # Speed: little endian, 8bit offset, 16bit, factor=0.1
    # raw=10000 → little endian bytes: 0x10, 0x27
    # DBC で BO_ 256 : 8 と宣言しているため data は 8 バイト必要
    frame = CanFrame(
        timestamp=0.0, channel=0, arbitration_id=256,
        is_extended_id=False, is_fd=False, is_rx=True,
        dlc=8, data_length=8, data=b"\x02\x10\x27\x00\x00\x00\x00\x00",
    )
    signals = loader.decode_frame(frame)
    by_name = {s.signal_name: s for s in signals}
    assert "State" in by_name
    assert "Speed" in by_name

    state = by_name["State"]
    assert state.raw_value == 2
    assert state.physical_value == 2.0
    # Value Table が引かれていること
    assert state.choice_text == "Running"

    speed = by_name["Speed"]
    assert speed.raw_value == 10000
    assert abs(speed.physical_value - 1000.0) < 1e-6
    assert speed.unit == "rpm"
    # Value Table 未定義
    assert speed.choice_text is None


def test_decode_frame_choice_label_unknown(tmp_path):
    """VAL_ 未定義の raw 値では choice_text=None"""
    loader = _make_loader(tmp_path)
    # State=99 (VAL_ に存在しない値)
    frame = CanFrame(
        timestamp=0.0, channel=0, arbitration_id=256,
        is_extended_id=False, is_fd=False, is_rx=True,
        dlc=8, data_length=8, data=b"\x63\x00\x00\x00\x00\x00\x00\x00",
    )
    signals = loader.decode_frame(frame)
    state = next(s for s in signals if s.signal_name == "State")
    assert state.raw_value == 99
    assert state.choice_text is None
