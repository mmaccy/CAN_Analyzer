"""ChartClickServer のテスト

実際のソケットを bind して HTTP リクエストを送り、HTML 配信とクリック
中継が機能することを確認する。
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from utils.click_server import ChartClickServer, build_plotly_html


@pytest.fixture
def server():
    s = ChartClickServer()
    s.start()
    # 起動待ち
    time.sleep(0.05)
    yield s
    s.stop()


def test_get_chart_html(server):
    """GET /chart.html で登録した HTML が返る"""
    server.set_html("<html><body>HELLO</body></html>")
    with urllib.request.urlopen(f"{server.url}") as resp:
        body = resp.read().decode("utf-8")
    assert "HELLO" in body
    assert resp.status == 200


def test_post_click_queues_time(server):
    """POST /click で送った時刻が drain_clicks() で取得できる"""
    payload = json.dumps({"time": 12.345}).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{server.port}/click",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 204

    clicks = server.drain_clicks()
    assert clicks == [12.345]
    # 二度目のドレインは空
    assert server.drain_clicks() == []


def test_multiple_clicks_preserved_in_order(server):
    """複数クリックがキュー順序で取り出せる"""
    for t in [1.0, 2.0, 3.5]:
        payload = json.dumps({"time": t}).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{server.port}/click",
            data=payload, method="POST",
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req).close()

    # サーバスレッドへの伝搬を少し待つ
    time.sleep(0.1)
    clicks = server.drain_clicks()
    assert clicks == [1.0, 2.0, 3.5]


def test_invalid_post_returns_400(server):
    """JSON でない body は 400"""
    req = urllib.request.Request(
        f"http://127.0.0.1:{server.port}/click",
        data=b"not json",
        method="POST",
    )
    try:
        urllib.request.urlopen(req)
        # urlopen は 4xx で例外を投げる
        pytest.fail("400 を期待したが成功してしまった")
    except urllib.error.HTTPError as e:
        assert e.code == 400


def test_unknown_path_returns_404(server):
    """未定義パスは 404"""
    req = urllib.request.Request(
        f"http://127.0.0.1:{server.port}/nope",
        method="GET",
    )
    try:
        urllib.request.urlopen(req)
        pytest.fail("404 を期待したが成功してしまった")
    except urllib.error.HTTPError as e:
        assert e.code == 404


def test_build_plotly_html_includes_click_bridge():
    """build_plotly_html が JS 中継スクリプトを HTML に埋め込む"""
    import plotly.graph_objects as go
    fig = go.Figure(data=[go.Scatter(x=[0, 1, 2], y=[10, 20, 15])])
    html = build_plotly_html(fig)
    # 埋込 JS の特徴文字列
    assert "plotly_click" in html
    assert "/click" in html
    # plotly.js が inline 同梱されている (CDN ではなく)
    assert "Plotly" in html
