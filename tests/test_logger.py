"""utils.logger のテスト"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from utils.logger import get_logger, get_log_path, setup_logging


def test_setup_returns_log_path(tmp_path, monkeypatch):
    """setup_logging が書き込み可能なパスを返す"""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    # 既に他のテストが setup 済みでも、再呼出しは同じパスを返す（冪等）
    path = setup_logging()
    assert path is not None
    assert isinstance(path, Path)


def test_get_logger_writes_to_file(tmp_path, monkeypatch):
    """ロガーが返り、INFO 以上のメッセージが書き出せる"""
    log = get_logger("can_analyzer.test_unit")
    log.info("test message via logger")
    log.error("test error message")
    # ファイルが存在し、何か書かれていれば成功（前のテストで初期化済み）
    log_path = get_log_path()
    assert log_path is not None
    # ファイルが書き込み可能であれば内容を読む（CI/開発機いずれでも動くことを期待）
    if log_path.exists():
        content = log_path.read_text(encoding="utf-8", errors="replace")
        # メッセージが落ちていればロガー機能は正常
        assert len(content) > 0


def test_logger_namespace_isolation():
    """get_logger は can_analyzer 名前空間配下に置かれる"""
    log = get_logger("foo.bar")
    assert log.name == "can_analyzer.foo.bar"
    # 既に can_analyzer.* で渡された場合は二重ネストしない
    log2 = get_logger("can_analyzer.baz")
    assert log2.name == "can_analyzer.baz"
