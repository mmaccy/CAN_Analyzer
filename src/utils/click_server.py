"""ブラウザ Plotly のクリックを Flet 側に伝える軽量ローカル HTTP サーバ

経路:
1. Flet 側で Plotly HTML を組み立て、`set_html()` でサーバに登録
2. ブラウザで `http://127.0.0.1:PORT/chart.html` を開く
3. HTML に埋め込まれた JS が `plotly_click` イベントで時刻を `POST /click` に送信
4. サーバはクリック時刻をキューに積み、Flet 側がポーリングで取り出す

実装方針:
- stdlib (`http.server`) のみで完結。FastAPI 等の依存を増やさない。
- ポートは OS に自動割当 (bind 0)。固定ポート競合を避ける。
- 127.0.0.1 のみで listen。LAN からアクセスされない。
- HTTPServer のサブクラスで HTML 文字列とクリックキューを保持。
"""

from __future__ import annotations

import http.server
import json
import queue
import threading
from typing import List, Optional

from utils.logger import get_logger


_log = get_logger(__name__)

# Plotly に埋め込む JS。post_script として fig.to_html() に渡す。
# この JS は plotly_click イベントを listen し、クリックされた時刻を
# POST /click に送る。fetch は失敗しても黙って捨てる (オフライン時等)。
_INJECTED_JS = r"""
(function() {
    function attach() {
        var gd = document.getElementsByClassName('plotly-graph-div')[0];
        if (!gd) {
            // plot 生成前なら次フレームで再試行
            requestAnimationFrame(attach);
            return;
        }
        gd.on('plotly_click', function(data) {
            if (!data || !data.points || data.points.length === 0) return;
            var t = Number(data.points[0].x);
            if (!isFinite(t)) return;
            fetch('/click', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({time: t})
            }).catch(function(e) { console.warn('click POST failed', e); });
        });
        console.log('CAN Analyzer click bridge attached');
    }
    attach();
})();
"""


class _Handler(http.server.BaseHTTPRequestHandler):
    """HTTP ハンドラ。ChartClickServer が `server` 属性で参照できる。"""

    # type: ChartClickServer (前方参照)
    server: "ChartClickServer"  # type: ignore[assignment]

    def do_GET(self) -> None:
        if self.path == "/chart.html" or self.path == "/" or self.path.startswith("/chart"):
            html = self.server.get_html()
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self) -> None:
        if self.path == "/click":
            length = int(self.headers.get("Content-Length", 0) or 0)
            try:
                body = self.rfile.read(length).decode("utf-8") if length else "{}"
                data = json.loads(body)
                t = float(data.get("time", 0))
                self.server.put_click(t)
                self.send_response(204)
                self.end_headers()
            except Exception as ex:
                _log.debug("POST /click 解釈失敗: %s", ex)
                self.send_response(400)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args) -> None:  # noqa: A002 (stdlib API)
        # 既定の stderr ログを抑制し、debug にダウングレード
        _log.debug("%s - %s", self.client_address[0], format % args)


class ChartClickServer(http.server.HTTPServer):
    """グラフクリック中継サーバ

    HTML を `set_html()` で更新し、クリックを `drain_clicks()` で取得する。
    `start()` で daemon スレッドにて serve_forever。
    """

    # クリックキュー上限 (バーストでも UI 側を壊さないように制限)
    _QUEUE_MAX = 100

    def __init__(self) -> None:
        self._html: str = "<html><body>No chart loaded yet.</body></html>"
        self._lock = threading.Lock()
        self._queue: "queue.Queue[float]" = queue.Queue(maxsize=self._QUEUE_MAX)
        self._thread: Optional[threading.Thread] = None
        # OS に空きポートを選ばせる
        super().__init__(("127.0.0.1", 0), _Handler)

    @property
    def port(self) -> int:
        return self.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/chart.html"

    def get_html(self) -> str:
        with self._lock:
            return self._html

    def set_html(self, html: str) -> None:
        with self._lock:
            self._html = html

    def put_click(self, t: float) -> None:
        try:
            self._queue.put_nowait(t)
        except queue.Full:
            _log.warning("クリックキューが満杯になりイベントを破棄")

    def drain_clicks(self) -> List[float]:
        out: List[float] = []
        while True:
            try:
                out.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return out

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self.serve_forever, daemon=True, name="ChartClickServer",
        )
        self._thread.start()
        _log.info("ChartClickServer 起動 (port=%d)", self.port)

    def stop(self) -> None:
        try:
            self.shutdown()
            self.server_close()
            _log.info("ChartClickServer 停止")
        except Exception:
            _log.exception("ChartClickServer 停止失敗")


def build_plotly_html(figure, plotly_config: Optional[dict] = None) -> str:
    """Plotly figure からクリック中継 JS を埋め込んだ自己完結 HTML を生成する。

    `plotly_config` は plotly.js の displaylogo 等の表示オプション。
    """
    return figure.to_html(
        include_plotlyjs="inline",
        full_html=True,
        config=plotly_config or {},
        post_script=_INJECTED_JS,
    )
