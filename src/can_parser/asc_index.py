"""ASC インデックスファイル (.asc.idx) の生成と読込

初回解析時に pickle+gzip で CanFrame リストとヘッダを保存し、
次回以降は（元 ASC ファイルが変更されていなければ）テキスト再パースをスキップして
インデックスから即座に復元する。GB 級ファイルで特に効果が大きい。

設計:
- 巨大ファイル (数百万フレーム) では単一 pickle.dump を 1 本の gzip ストリームに
  書くと、読込側で進捗が出せず固まったように見える。そこで
  メタデータ 1 pickle + フレームを CHUNK_SIZE ごとの複数 pickle に分割し、
  逐次 dump / load することで進捗通知を可能にする。
- 整合性は (schema_version, file_size, mtime_ns) で判定。
- 読込失敗 (EOF / 破損) 時はインデックスを無効化し、呼び出し元が再パースへフォールバック
  できるよう None を返す。
"""

import gzip
import pickle
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from models.can_frame import AscHeader, CanFrame


# スキーマ変更時にインクリメント（既存インデックスは無効化される）
# v3: CANFD の名前付き行を正しくパースするよう regex 修正 → v2 以前の
#     キャッシュは欠損フレームが残るため破棄
INDEX_SCHEMA_VERSION = 3

INDEX_SUFFIX = ".idx"

# フレーム 1 チャンクあたりの件数（進捗粒度と pickle サイズのトレードオフ）
CHUNK_SIZE = 10000


@dataclass
class AscIndex:
    """ASC パース結果のスナップショット（メモリ展開後の構造）"""
    schema_version: int = INDEX_SCHEMA_VERSION
    source_file_size: int = 0
    source_mtime_ns: int = 0
    header: Optional[AscHeader] = None
    frames: List[CanFrame] = field(default_factory=list)


def _index_path_for(asc_path: str) -> Path:
    return Path(asc_path + INDEX_SUFFIX)


def _source_signature(asc_path: str) -> tuple:
    st = Path(asc_path).stat()
    return (st.st_size, st.st_mtime_ns)


def load_index_if_valid(
    asc_path: str,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> Optional[AscIndex]:
    """既存インデックスを読み込む。元 ASC と整合しない場合や破損時は None を返す。

    Args:
        asc_path: 元 ASC ファイルのパス
        progress_callback: (読込済みフレーム数, 総フレーム数) を受けるコールバック
    """
    idx_path = _index_path_for(asc_path)
    if not idx_path.exists():
        return None

    try:
        size, mtime_ns = _source_signature(asc_path)
    except OSError:
        return None

    try:
        with gzip.open(idx_path, "rb") as f:
            meta = pickle.load(f)
            if (
                not isinstance(meta, dict)
                or meta.get("schema_version") != INDEX_SCHEMA_VERSION
                or meta.get("source_file_size") != size
                or meta.get("source_mtime_ns") != mtime_ns
            ):
                return None
            header: Optional[AscHeader] = meta.get("header")
            total = int(meta.get("frame_count", 0))

            frames: List[CanFrame] = []
            # チャンク pickle を EOF まで逐次読み込む
            while True:
                try:
                    chunk = pickle.load(f)
                except EOFError:
                    break
                if not isinstance(chunk, list):
                    # 想定外フォーマットは破棄
                    return None
                frames.extend(chunk)
                if progress_callback and total > 0:
                    progress_callback(min(len(frames), total), total)
            if total and len(frames) != total:
                # 書き出し途中で中断されたインデックス等、一貫性なし → 破棄
                return None
    except Exception:
        # 破損インデックス・旧フォーマット等はすべて再生成に任せる
        return None

    return AscIndex(
        schema_version=INDEX_SCHEMA_VERSION,
        source_file_size=size,
        source_mtime_ns=mtime_ns,
        header=header,
        frames=frames,
    )


def save_index(
    asc_path: str,
    header: AscHeader,
    frames: List[CanFrame],
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> None:
    """インデックスを書き出す（チャンク分割 + 進捗通知対応）。

    I/O エラーは再生成可能な副次データなので握りつぶし、呼び出し元には伝えない。
    書き込み失敗時は不完全ファイルを残さないよう一時パスに書き出してからリネームする。
    """
    try:
        size, mtime_ns = _source_signature(asc_path)
    except OSError:
        return

    idx_path = _index_path_for(asc_path)
    tmp_path = idx_path.with_suffix(idx_path.suffix + ".tmp")
    total = len(frames)

    try:
        with gzip.open(tmp_path, "wb", compresslevel=3) as f:
            # 1) メタデータ
            pickle.dump(
                {
                    "schema_version": INDEX_SCHEMA_VERSION,
                    "source_file_size": size,
                    "source_mtime_ns": mtime_ns,
                    "header": header,
                    "frame_count": total,
                },
                f,
                protocol=pickle.HIGHEST_PROTOCOL,
            )
            # 2) フレームをチャンク単位で dump
            for i in range(0, total, CHUNK_SIZE):
                chunk = frames[i : i + CHUNK_SIZE]
                pickle.dump(chunk, f, protocol=pickle.HIGHEST_PROTOCOL)
                if progress_callback:
                    progress_callback(min(i + len(chunk), total), total)
        # 原子的置換
        tmp_path.replace(idx_path)
    except OSError:
        try:
            tmp_path.unlink()
        except OSError:
            pass


def delete_index(asc_path: str) -> None:
    """インデックスを削除する（破損検出時等のリセット用）"""
    try:
        _index_path_for(asc_path).unlink()
    except OSError:
        pass
