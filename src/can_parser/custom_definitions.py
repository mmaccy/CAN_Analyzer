"""カスタムシグナル定義 — JSON ファイルによるメッセージ/シグナルの追加・上書き

ARXML/DBC に無いシグナルを追加したり、既存定義を修正して cantools Database に
注入するためのモジュール。

JSON 形式:
{
  "version": 1,
  "messages": [
    {
      "frame_id": "0x10E1AA1F",   // hex 文字列 or 10進整数
      "name": "SCMS_10E1AA1F_FD",
      "length": 64,
      "is_fd": true,
      "is_extended_frame": true,
      "cycle_time_ms": 100,        // 省略可。GRP-12 ギャップ検出に使用
      "override": true,             // true: 既存定義を置換, false: 新規追加のみ
      "signals": [
        {
          "name": "SCMS_CMSFAILURE",
          "start_bit": 7,
          "length": 1,
          "byte_order": "big_endian",  // "big_endian" | "little_endian"
          "is_signed": false,
          "scale": 1,
          "offset": 0,
          "unit": "-",
          "minimum": 0,
          "maximum": 1,
          "choices": {               // Value Table (省略可)
            "0": "Normal",
            "1": "Failure"
          }
        }
      ]
    }
  ]
}
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import cantools
from cantools.database.conversion import (
    LinearConversion,
    NamedSignalConversion,
)

_log = logging.getLogger(__name__)


def _parse_frame_id(raw: Any) -> int:
    """frame_id を hex 文字列 / 10進整数のいずれからでもパースする"""
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        raw = raw.strip()
        if raw.lower().startswith("0x"):
            return int(raw, 16)
        return int(raw)
    raise ValueError(f"frame_id の型が不正: {type(raw)} ({raw!r})")


def _build_signal(sig_def: dict) -> cantools.database.can.Signal:
    """JSON のシグナル定義辞書から cantools Signal を生成する"""
    choices_raw = sig_def.get("choices")
    scale = sig_def.get("scale", 1)
    offset = sig_def.get("offset", 0)
    # is_float は「ビットパターンを IEEE754 浮動小数点として解釈する」フラグ。
    # 通常の scale/offset 変換とは無関係。16/32/64 bit 以外で True にすると
    # bitstruct が "expected float size of 16, 32, or 64 bits" エラーを出す。
    # JSON で明示指定されない限り False (整数アンパック → scale/offset 変換)。
    is_float = sig_def.get("is_float", False)

    if choices_raw:
        # キーを int に統一
        choices = {int(k): str(v) for k, v in choices_raw.items()}
        conversion = NamedSignalConversion(
            scale=scale,
            offset=offset,
            choices=choices,
            is_float=is_float,
        )
    else:
        conversion = LinearConversion(
            scale=scale,
            offset=offset,
            is_float=is_float,
        )

    byte_order = sig_def.get("byte_order", "little_endian")

    return cantools.database.can.Signal(
        name=sig_def["name"],
        start=sig_def["start_bit"],
        length=sig_def["length"],
        byte_order=byte_order,
        is_signed=sig_def.get("is_signed", False),
        conversion=conversion,
        minimum=sig_def.get("minimum"),
        maximum=sig_def.get("maximum"),
        unit=sig_def.get("unit", ""),
        comment=sig_def.get("comment"),
    )


def _build_message(msg_def: dict) -> cantools.database.can.Message:
    """JSON のメッセージ定義辞書から cantools Message を生成する"""
    signals = [_build_signal(s) for s in msg_def.get("signals", [])]
    frame_id = _parse_frame_id(msg_def["frame_id"])
    cycle_time = msg_def.get("cycle_time_ms")

    return cantools.database.can.Message(
        frame_id=frame_id,
        name=msg_def.get("name", f"CUSTOM_{frame_id:X}"),
        length=msg_def.get("length", 8),
        signals=signals,
        cycle_time=cycle_time,
        is_extended_frame=msg_def.get("is_extended_frame", False),
        is_fd=msg_def.get("is_fd", False),
    )


def load_custom_definitions(
    file_path: str,
    db: cantools.database.Database,
) -> int:
    """JSON ファイルからカスタム定義を読み込み、cantools Database に注入する。

    - override=true のメッセージ: 既存メッセージを削除してから追加
    - override=false (デフォルト): 既存メッセージが無い場合のみ追加

    Returns:
        追加/置換されたメッセージ数
    """
    path = Path(file_path)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    version = data.get("version", 1)
    if version != 1:
        _log.warning("カスタム定義ファイルのバージョン %s は未対応", version)

    count = 0
    for msg_def in data.get("messages", []):
        try:
            new_msg = _build_message(msg_def)
            override = msg_def.get("override", False)

            # 既存メッセージの確認
            existing = None
            try:
                existing = db.get_message_by_frame_id(new_msg.frame_id)
            except KeyError:
                pass

            if existing is not None:
                if override:
                    # 既存メッセージを _messages リストからも除去
                    db._messages = [
                        m for m in db._messages
                        if m.frame_id != new_msg.frame_id
                    ]
                    _log.info(
                        "カスタム定義: メッセージ上書き %s (0x%X) → %d signals",
                        new_msg.name, new_msg.frame_id, len(new_msg.signals),
                    )
                else:
                    _log.info(
                        "カスタム定義: メッセージ %s (0x%X) は既存のためスキップ "
                        "(override=true で上書き可)",
                        existing.name, existing.frame_id,
                    )
                    continue

            # _add_message は辞書のみ更新し _messages リストに追加しないため、
            # 明示的にリストにも追加する。
            db._add_message(new_msg)
            db._messages.append(new_msg)
            count += 1
            _log.info(
                "カスタム定義: メッセージ追加 %s (0x%X) signals=%d cycle=%s",
                new_msg.name, new_msg.frame_id,
                len(new_msg.signals),
                new_msg.cycle_time,
            )
        except Exception as ex:
            _log.error("カスタム定義のメッセージ解析エラー: %s", ex)

    return count


def export_message_to_custom_json(
    db: cantools.database.Database,
    frame_id: int,
    output_path: Optional[str] = None,
) -> dict:
    """既存メッセージ定義をカスタム JSON 形式で出力する。

    修正のベースとして使用する。output_path 指定時はファイルにも書き出す。
    """
    msg = db.get_message_by_frame_id(frame_id)
    signals = []
    for s in msg.signals:
        sig_dict = {
            "name": s.name,
            "start_bit": s.start,
            "length": s.length,
            "byte_order": s.byte_order,
            "is_signed": s.is_signed,
            "scale": s.scale,
            "offset": s.offset,
            "unit": s.unit or "",
            "minimum": s.minimum,
            "maximum": s.maximum,
        }
        if s.choices:
            sig_dict["choices"] = {str(k): str(v) for k, v in s.choices.items()}
        if s.comment:
            sig_dict["comment"] = s.comment
        signals.append(sig_dict)

    msg_dict = {
        "frame_id": f"0x{msg.frame_id:X}",
        "name": msg.name,
        "length": msg.length,
        "is_fd": msg.is_fd,
        "is_extended_frame": msg.is_extended_frame,
        "cycle_time_ms": msg.cycle_time,
        "override": True,
        "signals": signals,
    }

    result = {"version": 1, "messages": [msg_dict]}

    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        _log.info("カスタム定義エクスポート: %s → %s", msg.name, output_path)

    return result


def create_template(output_path: str) -> None:
    """空のカスタム定義テンプレートファイルを生成する"""
    template = {
        "version": 1,
        "_comment": "カスタムシグナル定義テンプレート。messages 配列にメッセージ/シグナルを追加してください。",
        "messages": [
            {
                "frame_id": "0x100",
                "name": "EXAMPLE_MSG",
                "length": 8,
                "is_fd": False,
                "is_extended_frame": False,
                "cycle_time_ms": 100,
                "override": False,
                "signals": [
                    {
                        "name": "EXAMPLE_SIGNAL",
                        "start_bit": 0,
                        "length": 8,
                        "byte_order": "little_endian",
                        "is_signed": False,
                        "scale": 1,
                        "offset": 0,
                        "unit": "",
                        "minimum": 0,
                        "maximum": 255,
                        "choices": {
                            "0": "OFF",
                            "1": "ON",
                        },
                    }
                ],
            }
        ],
    }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(template, f, ensure_ascii=False, indent=2)
    _log.info("カスタム定義テンプレート作成: %s", output_path)


def export_all_messages_to_json(
    db: cantools.database.Database,
    output_path: str,
) -> int:
    """DB 内の全メッセージ定義をカスタム JSON 形式でエクスポートする。

    Returns:
        エクスポートされたメッセージ数
    """
    messages = []
    for msg in db.messages:
        signals = []
        for s in msg.signals:
            sig_dict = {
                "name": s.name,
                "start_bit": s.start,
                "length": s.length,
                "byte_order": s.byte_order,
                "is_signed": s.is_signed,
                "scale": s.scale,
                "offset": s.offset,
                "unit": s.unit or "",
                "minimum": s.minimum,
                "maximum": s.maximum,
            }
            if s.choices:
                sig_dict["choices"] = {str(k): str(v) for k, v in s.choices.items()}
            if s.comment:
                sig_dict["comment"] = s.comment
            signals.append(sig_dict)

        msg_dict = {
            "frame_id": f"0x{msg.frame_id:X}",
            "name": msg.name,
            "length": msg.length,
            "is_fd": msg.is_fd,
            "is_extended_frame": msg.is_extended_frame,
            "cycle_time_ms": msg.cycle_time,
            "override": True,
            "signals": signals,
        }
        messages.append(msg_dict)

    result = {"version": 1, "messages": messages}
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    _log.info("全メッセージ JSON エクスポート: %d messages → %s", len(messages), output_path)
    return len(messages)


def export_db_as_dbc(
    db: cantools.database.Database,
    output_path: str,
) -> int:
    """DB の全メッセージ定義を DBC 形式でエクスポートする。

    Returns:
        エクスポートされたメッセージ数
    """
    dbc_string = db.as_dbc_string()
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(dbc_string)
    count = len(db.messages)
    _log.info("DBC エクスポート: %d messages → %s", count, output_path)
    return count


def _escape_arxml(text: str) -> str:
    """ARXML 用 XML エスケープ"""
    return (text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&apos;"))


def export_db_as_arxml(
    db: cantools.database.Database,
    output_path: str,
) -> int:
    """DB の全メッセージ定義を AUTOSAR ARXML 形式でエクスポートする。

    cantools には as_arxml_string がないため、独自に ARXML 3.x 形式を生成する。
    生成される ARXML は再インポート可能な最小限のフォーマット。

    Returns:
        エクスポートされたメッセージ数
    """
    lines: List[str] = []
    lines.append('<?xml version="1.0" encoding="UTF-8"?>')
    lines.append('<AUTOSAR xmlns="http://autosar.org/schema/r4.0"')
    lines.append('  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">')
    lines.append('  <AR-PACKAGES>')
    lines.append('    <AR-PACKAGE>')
    lines.append('      <SHORT-NAME>CAN_DB</SHORT-NAME>')
    lines.append('      <ELEMENTS>')

    for msg in db.messages:
        _emit_i_signal_i_pdu(lines, msg)

    lines.append('      </ELEMENTS>')
    lines.append('    </AR-PACKAGE>')

    # システムシグナルパッケージ (Value Table 用 COMPU-METHOD)
    lines.append('    <AR-PACKAGE>')
    lines.append('      <SHORT-NAME>CompuMethods</SHORT-NAME>')
    lines.append('      <ELEMENTS>')
    for msg in db.messages:
        for s in msg.signals:
            if s.choices:
                _emit_compu_method(lines, s)
    lines.append('      </ELEMENTS>')
    lines.append('    </AR-PACKAGE>')

    lines.append('  </AR-PACKAGES>')
    lines.append('</AUTOSAR>')

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    count = len(db.messages)
    _log.info("ARXML エクスポート: %d messages → %s", count, output_path)
    return count


def _emit_i_signal_i_pdu(lines: List[str], msg) -> None:
    """1 メッセージ分の I-SIGNAL-I-PDU を出力する"""
    esc = _escape_arxml
    lines.append(f'        <I-SIGNAL-I-PDU>')
    lines.append(f'          <SHORT-NAME>{esc(msg.name)}</SHORT-NAME>')
    lines.append(f'          <LENGTH>{msg.length * 8}</LENGTH>')

    if msg.cycle_time is not None and msg.cycle_time > 0:
        lines.append(f'          <I-PDU-TIMING-SPECIFICATIONS>')
        lines.append(f'            <I-PDU-TIMING>')
        lines.append(f'              <TRANSMISSION-MODE-DECLARATION>')
        lines.append(f'                <TRANSMISSION-MODE-TRUE-TIMING>')
        lines.append(f'                  <CYCLIC-TIMING>')
        lines.append(f'                    <TIME-PERIOD>')
        lines.append(f'                      <VALUE>{msg.cycle_time / 1000.0:.6f}</VALUE>')
        lines.append(f'                    </TIME-PERIOD>')
        lines.append(f'                  </CYCLIC-TIMING>')
        lines.append(f'                </TRANSMISSION-MODE-TRUE-TIMING>')
        lines.append(f'              </TRANSMISSION-MODE-DECLARATION>')
        lines.append(f'            </I-PDU-TIMING>')
        lines.append(f'          </I-PDU-TIMING-SPECIFICATIONS>')

    lines.append(f'          <I-SIGNAL-TO-PDU-MAPPINGS>')
    for s in msg.signals:
        bit_pos = s.start
        byte_order = "MOST-SIGNIFICANT-BYTE-LAST" if s.byte_order == "little_endian" else "MOST-SIGNIFICANT-BYTE-FIRST"
        lines.append(f'            <I-SIGNAL-TO-I-PDU-MAPPING>')
        lines.append(f'              <SHORT-NAME>{esc(s.name)}_Mapping</SHORT-NAME>')
        lines.append(f'              <I-SIGNAL-REF DEST="I-SIGNAL">/Signals/{esc(s.name)}</I-SIGNAL-REF>')
        lines.append(f'              <PACKING-BYTE-ORDER>{byte_order}</PACKING-BYTE-ORDER>')
        lines.append(f'              <START-POSITION>{bit_pos}</START-POSITION>')
        lines.append(f'            </I-SIGNAL-TO-I-PDU-MAPPING>')
    lines.append(f'          </I-SIGNAL-TO-PDU-MAPPINGS>')

    # ADMIN-DATA にフレーム ID を記録 (再インポート時の参照用)
    lines.append(f'          <ADMIN-DATA>')
    lines.append(f'            <SDGS>')
    lines.append(f'              <SDG GID="CAN">')
    lines.append(f'                <SD GID="FrameId">0x{msg.frame_id:X}</SD>')
    lines.append(f'                <SD GID="IsExtended">{"true" if msg.is_extended_frame else "false"}</SD>')
    lines.append(f'                <SD GID="IsFD">{"true" if msg.is_fd else "false"}</SD>')
    lines.append(f'                <SD GID="DLC">{msg.length}</SD>')
    lines.append(f'              </SDG>')
    lines.append(f'            </SDGS>')
    lines.append(f'          </ADMIN-DATA>')
    lines.append(f'        </I-SIGNAL-I-PDU>')

    # 各シグナルも I-SIGNAL として出力
    for s in msg.signals:
        _emit_i_signal(lines, s)


def _emit_i_signal(lines: List[str], sig) -> None:
    """1 シグナル分の I-SIGNAL を出力する"""
    esc = _escape_arxml
    lines.append(f'        <I-SIGNAL>')
    lines.append(f'          <SHORT-NAME>{esc(sig.name)}</SHORT-NAME>')
    lines.append(f'          <I-SIGNAL-TYPE>PRIMITIVE</I-SIGNAL-TYPE>')
    lines.append(f'          <INIT-VALUE>')
    lines.append(f'            <NUMERICAL-VALUE-SPECIFICATION>')
    lines.append(f'              <VALUE>0</VALUE>')
    lines.append(f'            </NUMERICAL-VALUE-SPECIFICATION>')
    lines.append(f'          </INIT-VALUE>')
    lines.append(f'          <LENGTH>{sig.length}</LENGTH>')

    # COMPU-METHOD 参照 (Value Table がある場合)
    if sig.choices:
        lines.append(f'          <COMPU-METHOD-REF DEST="COMPU-METHOD">/CompuMethods/{esc(sig.name)}_CM</COMPU-METHOD-REF>')

    # 物理変換情報をコメントに記録
    lines.append(f'          <!-- scale={sig.scale} offset={sig.offset} unit="{esc(sig.unit or "")}" '
                 f'min={sig.minimum} max={sig.maximum} signed={sig.is_signed} -->')
    lines.append(f'        </I-SIGNAL>')


def _emit_compu_method(lines: List[str], sig) -> None:
    """Value Table を COMPU-METHOD (TEXTTABLE) として出力する"""
    esc = _escape_arxml
    lines.append(f'        <COMPU-METHOD>')
    lines.append(f'          <SHORT-NAME>{esc(sig.name)}_CM</SHORT-NAME>')
    lines.append(f'          <CATEGORY>TEXTTABLE</CATEGORY>')
    lines.append(f'          <COMPU-INTERNAL-TO-PHYS>')
    lines.append(f'            <COMPU-SCALES>')
    for raw_val, label in sorted(sig.choices.items()):
        lines.append(f'              <COMPU-SCALE>')
        lines.append(f'                <LOWER-LIMIT INTERVAL-TYPE="CLOSED">{raw_val}</LOWER-LIMIT>')
        lines.append(f'                <UPPER-LIMIT INTERVAL-TYPE="CLOSED">{raw_val}</UPPER-LIMIT>')
        lines.append(f'                <COMPU-CONST>')
        lines.append(f'                  <VT>{esc(str(label))}</VT>')
        lines.append(f'                </COMPU-CONST>')
        lines.append(f'              </COMPU-SCALE>')
    lines.append(f'            </COMPU-SCALES>')
    lines.append(f'          </COMPU-INTERNAL-TO-PHYS>')
    if sig.unit:
        lines.append(f'          <UNIT-REF>{esc(sig.unit)}</UNIT-REF>')
    lines.append(f'        </COMPU-METHOD>')
