# CAN FD Log Analyzer — タスク管理

## Phase 1: オフライン解析 MVP

- [x] プロジェクト構造・requirements.txt 作成
- [x] データモデル (CanFrame, SignalValue, FrameStatistics)
- [x] ASC パーサ (Classic CAN + CAN FD ストリーミング)
- [x] ASC エクスポート (フィルタ付きフレーム抽出)
- [x] DBC 読込 (cantools ベース)
- [x] Flet メインウィンドウ + タブレイアウト
- [x] トレースビュー (テーブル + フィルタ + ページング)
- [x] シグナルツリー (DBC フレーム/シグナル階層)
- [x] フレーム抽出エクスポート
- [x] プログレスバー (ファイル読込)

## Phase 2: グラフ・統計

- [x] 時系列グラフ (Plotly + ft.PlotlyChart)
- [x] 複数シグナル同時表示 (オーバーレイ / サブプロット)
- [x] グラフ操作 (ズーム/カーソル/範囲選択 — Plotly built-in)
- [x] 統計情報算出 (周期・バスロード・メッセージカウント)
- [x] 統計パネル UI
- [x] シグナルデコード表示 (トレース行展開)
- [x] インデックスファイル生成 / 活用 (大容量最適化)
- [x] 仮想スクロール改善
- [x] CanFrame メモリ最適化 (slots, raw_line 廃止)
- [x] DB 定義フレームのみの ASC エクスポート (EXP-9)

## Phase 3: リアルタイム CAN 受信

- [x] python-can Vector / Virtual インターフェース接続 (`realtime/can_receiver.py`)
- [x] 接続設定ダイアログ (`gui/connection_dialog.py`)
- [x] リアルタイムトレースビュー (`TracePanel.add_frames`)
- [x] リアルタイムログ記録 (ASC 出力 — `asc_writer.format_frame_as_asc`)
- [x] リアルタイムデコード表示 (既存トレース選択行のシグナルデコードを流用)
- [x] リアルタイムグラフ表示 (`GraphPanel.add_frames` + `refresh_live()` を 1Hz 再描画)
- [x] 受信フィルタ (RT-4: `TracePanel.add_frames` 内で `_frame_matches_filter` 適用済み)
- [ ] Vector 実機での結合テスト (要 XL Driver / VN1610)

## Phase 2.5: グラフ・ツリー UX 改善

- [x] グラフ: フレーム途絶の可視化 (ARXML/DBC 定義周期の 5 倍以上のギャップはライン非描画)
- [x] グラフ: 凡例クリックでシグナル強調表示 (複数選択、非選択は不透過 30%、空クリックでリセット)
- [x] シグナルツリー: 選択中シグナルをフレーム折りたたみ時も常時表示
- [x] 設定の保存/読込 (選択シグナル等を JSON で永続化)

## Phase 4: 品質向上

- [x] 単体テスト整備 (`tests/test_asc_writer_format.py`, `test_can_receiver.py`, `test_click_server.py`, `test_dbc_loader.py`, `test_statistics.py`, `test_logger.py` — 全 31 テスト)
- [x] カーソル同期: 双方向。トレース→グラフは赤縦線、グラフ→トレースは アプリ内 SVG クリック (`GestureDetector`) と ブラウザ Plotly クリック (`utils/click_server.py` 経由) の両方で時刻ジャンプ
- [x] Value Table デコード (`SignalValue.choice_text`、トレース詳細パネルで `数値 (ラベル)` 形式表示)
- [x] 統計 CSV エクスポート (`statistics_panel.write_statistics_csv` + UI ボタン)
- [x] キーボードショートカット (`MainWindow._on_keyboard_event` — Ctrl+O/D/E/S/L/K, Ctrl+Shift+E/K, Ctrl+1/2/3, F5)
- [x] エラーハンドリング改善 (`utils/logger.py` 集約ロガー + `_show_error()` ヘルパ + ツールバーから「ログを開く」ボタン)
