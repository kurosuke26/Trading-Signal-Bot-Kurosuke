#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
collector.py — データ収集・判定バッチ（2:00〜6:00 JST頃に実行する想定）

【今回の変更の背景】
以前は「データ取得」と「Discord投稿」を1本のスクリプト（毎朝7:00起動）で
まとめて行っていたが、全銘柄（約3,900）を対象にすると投稿までの反映が遅くなる
上、短時間に大量リクエストを送るとYahoo! Finance側のレート制限（ブロック）に
かかりやすい。

そこで本バージョンから処理を2段階に分割した:

  1. collector.py（本ファイル）：深夜2:00〜6:00 JSTの間に、意図的にゆっくり
     ペースを配分しながら全銘柄のデータ取得・判定を行い、結果を
     data/latest_scan.json に保存する（Discordへは投稿しない）。
  2. poster.py：朝7:00〜8:00 JSTに、保存済みのJSONを読み込んで整形し、
     Discordの5チャネルに投稿する（ネットワーク負荷はほぼゼロ）。

【なぜデータが「古くならない」か】
東証の取引時間は 9:00〜15:30 JST。前日の取引終了（15:30 JST）から翌営業日の
寄り付き（9:00 JST）までは株価・出来高などのデータは更新されないため、
深夜2:00に取得しても朝7:00に取得しても、Yahoo! Finance側の値は基本的に同じ
（＝前日終値ベース）になる。したがって収集タイミングを早めても情報が古くなる
デメリットは実質的に生じない。

【ペーシングの考え方】
一気に全銘柄を並列で叩くのではなく、FUNDAMENTALS_WINDOW_MINUTES /
HISTORY_WINDOW_MINUTES で指定した時間（既定：150分 + 70分 ＝ 220分、
2:00開始なら5:40頃に終わる想定）に均等に引き延ばしながら取得する
（util.pace_to_target）。これとは別に、想定外に遅れた場合の非常停止ライン
として TIME_BUDGET_MINUTES（既定230分）を設けており、超過分は打ち切って
そこまでの結果を保存する。

なお、この目標時間（150分・65分）は「全銘柄（約3900銘柄）を対象にした本番実行」を
想定したものであり、MAX_TICKERSで対象銘柄数を絞ったテスト実行では
scaled_window_minutes() により対象銘柄数に比例して自動的に短縮される。

【正直な注意点】
このスクリプトを開発したサンドボックス環境はネットワークが制限されており、
Yahoo! Finance にも JPX にも直接アクセスできなかったため、実データに対して
通しで動作確認ができていません。必ずテスト用サーバー・MAX_TICKERSを絞った状態
で一度動かし、ログとdata/latest_scan.jsonの中身を確認してください。
"""

import concurrent.futures
import json
import os
import sys
import time
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf

from indicators import compute_technical_snapshot, technical_score
from sakata import detect_all_patterns, sakata_score
from scoring import composite_score
from universe import get_all_tse_tickers, FALLBACK_TICKERS
from util import env_int, env_float, json_default, pace_to_target

# ---------------------------------------------------------------------------
# 実行パラメータ（環境変数で調整可能）
# ---------------------------------------------------------------------------
MAX_TICKERS = env_int('MAX_TICKERS', 0)                       # 0 = 制限なし（全銘柄）
HISTORY_PERIOD = os.getenv('HISTORY_PERIOD') or '9mo'         # MA75/MACDに必要な日数を確保

# 非常停止ライン（想定外に処理が遅れた場合、ここで強制的に打ち切る）
TIME_BUDGET_MINUTES = env_float('TIME_BUDGET_MINUTES', 230)

# 意図的にゆっくり進めるためのペーシング目標時間（合計が概ねTIME_BUDGET_MINUTES以下になるように）
# ※ これは「全銘柄（約3900銘柄）を対象にした本番実行」を想定した時間。MAX_TICKERSで
#   件数を絞ったテスト実行では、下の scaled_window_minutes() で対象銘柄数に比例して
#   自動的に短縮する（さもないと50銘柄のテストでも数時間かかってしまうため）。
FUNDAMENTALS_WINDOW_MINUTES = env_float('FUNDAMENTALS_WINDOW_MINUTES', 150)
HISTORY_WINDOW_MINUTES = env_float('HISTORY_WINDOW_MINUTES', 65)

# 上のペーシング時間が「本番実行」として想定している銘柄数の目安（東証全体・約3900銘柄）。
# テスト実行などで対象銘柄数がこれより少ない場合、ペーシング時間を比例配分で短縮する。
REFERENCE_UNIVERSE_SIZE = env_int('REFERENCE_UNIVERSE_SIZE', 3900)

FETCH_WORKERS = env_int('FETCH_WORKERS', 4)                   # 同時並列数（控えめに）
HISTORY_CHUNK_SIZE = env_int('HISTORY_CHUNK_SIZE', 100)
INFO_CHUNK_SIZE = env_int('INFO_CHUNK_SIZE', 30)
TOP_NEUTRAL_KEEP = env_int('TOP_NEUTRAL_KEEP', 20)             # ニュートラルのうち詳細を残す上位件数
OUTPUT_PATH = os.getenv('SCAN_OUTPUT_PATH') or 'data/latest_scan.json'

_START_TIME = time.time()


def _elapsed_minutes():
    return (time.time() - _START_TIME) / 60.0


def _over_budget():
    return _elapsed_minutes() > TIME_BUDGET_MINUTES


def scaled_window_minutes(base_minutes, ticker_count, reference=REFERENCE_UNIVERSE_SIZE, min_minutes=1.0):
    """
    ペーシングの目標時間（base_minutes）を、対象銘柄数（ticker_count）が
    本番想定件数（reference、既定3900）よりどれだけ少ないかに応じて比例配分で短縮する。

    例：base_minutes=150分、reference=3900件のとき、
        - 対象50件（MAX_TICKERSでのテスト実行）なら 150 * 50/3900 ≒ 1.9分
        - 対象3900件（全銘柄・本番実行）なら 150分（そのまま）
        - 対象がreferenceを超える場合も base_minutes を上限として扱う（それ以上は伸ばさない）

    これが無いと、MAX_TICKERSで件数を絞った「数分で終わるはずのテスト実行」でも、
    本番と同じ150分・65分という時間をかけてペーシングしてしまい、テストの意味が薄れる。
    """
    if reference <= 0 or ticker_count <= 0:
        return base_minutes
    scaled = base_minutes * (ticker_count / reference)
    return max(min_minutes, min(base_minutes, scaled))


# ---------------------------------------------------------------------------
# EPS3期推移（LONG候補のみ深掘り取得）
# ---------------------------------------------------------------------------
def get_eps_trend(stock):
    """直近の年次EPSが3期連続で維持〜増加しているかを判定。取得不可ならNone。"""
    try:
        stmt = stock.income_stmt
        if stmt is None or stmt.empty:
            return None

        eps_row = None
        for label in ['Diluted EPS', 'Basic EPS']:
            if label in stmt.index:
                eps_row = stmt.loc[label]
                break
        if eps_row is None:
            candidates = [idx for idx in stmt.index if 'eps' in str(idx).lower()]
            if candidates:
                eps_row = stmt.loc[candidates[0]]
        if eps_row is None:
            return None

        eps_row = eps_row.dropna()
        if len(eps_row) < 3:
            return None

        cols_sorted = sorted(eps_row.index)
        values = [float(eps_row[c]) for c in cols_sorted][-3:]
        increasing = all(values[i] <= values[i + 1] for i in range(len(values) - 1))
        return {'values': values, 'increasing': increasing}
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 銘柄ユニバース・株価履歴・ファンダメンタルズの取得
# ---------------------------------------------------------------------------
def resolve_universe():
    tickers, meta_df = get_all_tse_tickers()
    used_fallback = (meta_df is None and tickers == FALLBACK_TICKERS)
    if MAX_TICKERS and MAX_TICKERS > 0 and len(tickers) > MAX_TICKERS:
        print(f'[universe] MAX_TICKERS={MAX_TICKERS} が設定されているため先頭{MAX_TICKERS}銘柄に絞り込みます（テスト用）')
        tickers = tickers[:MAX_TICKERS]
    return tickers, meta_df, used_fallback


def fetch_price_histories(tickers, period=HISTORY_PERIOD, chunk_size=HISTORY_CHUNK_SIZE,
                           max_retries=2, window_minutes=HISTORY_WINDOW_MINUTES):
    """株価履歴（OHLCV）をチャンク単位でバッチ取得する。チャンクごとにペーシングをかける。"""
    histories = {}
    failed = []
    total_chunks = (len(tickers) + chunk_size - 1) // chunk_size
    phase_start = time.time()
    target_seconds = window_minutes * 60

    for ci in range(total_chunks):
        if _over_budget():
            remaining = tickers[ci * chunk_size:]
            print(f'[history] 時間予算（{TIME_BUDGET_MINUTES}分）超過のため残り{len(remaining)}銘柄の株価取得をスキップします')
            failed.extend([(t, '時間予算超過によりスキップ') for t in remaining])
            break

        chunk = tickers[ci * chunk_size:(ci + 1) * chunk_size]
        data = None
        for attempt in range(max_retries):
            try:
                data = yf.download(tickers=chunk, period=period, interval='1d',
                                    group_by='ticker', threads=True, progress=False,
                                    auto_adjust=False)
                break
            except Exception as e:
                print(f'[history] チャンク{ci + 1}/{total_chunks} 取得失敗（試行{attempt + 1}）: {e}')
                if attempt < max_retries - 1:
                    time.sleep(3 * (attempt + 1))

        if data is None:
