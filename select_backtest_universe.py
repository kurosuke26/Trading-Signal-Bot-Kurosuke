#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
select_backtest_universe.py — 本番の data/latest_scan.json（東証全銘柄スキャン結果）から、
バックテスト用に300〜500銘柄程度の「LONG候補を意図的に多く含む」ユニバースを選び、
JQUANTS_TICKERS用のカンマ区切り文字列を作る。

【背景】
Phase 1（15銘柄・大型株中心）ではLONGが43件しか集まらず、SHORT(317件)に偏った。
これは大型株は配当利回り3.5〜5.8%＋PER×PBR≦22.5というLONG条件に収まりにくいため。
本番が2026-09-02に全銘柄スキャンした結果（signal別のdividend_yield/per_pbrを含む）を
流用すれば、「その時点でLONG判定だった銘柄」を効率よく抽出でき、中小型・高配当銘柄を
狙い撃ちでバックテスト対象に含められる。

【選定方法】
1. signal=='LONG'の銘柄は全件採用（本番の一次スクリーニングを通過した「LONGになりやすい」銘柄）
2. signal=='SHORT'の銘柄からLONGと同数程度をランダム抽出（比較用）
3. 残り枠をsignal=='NEUTRAL'からランダム抽出して埋める（target_total件になるまで）
※ あくまで初期ユニバースの選び方であり、実際のバックテストでは日々のLONG/SHORT判定は
  過去データに対して毎回ゼロから再計算される（2026-09-02時点の判定結果を鵜呑みにする
  わけではない）。

【実行方法】
  python select_backtest_universe.py
  → 標準出力にカンマ区切りのティッカーリストを表示する。これを
    $env:JQUANTS_TICKERS="..." にそのままコピペして使う。
  （target_total等を変えたい場合は環境変数 BACKTEST_UNIVERSE_SIZE で指定可能。既定400）
"""

import json
import os
import random

SCAN_PATH = os.getenv('SCAN_OUTPUT_PATH') or 'data/latest_scan.json'
TARGET_TOTAL = int(os.getenv('BACKTEST_UNIVERSE_SIZE', '400'))
random.seed(42)


def to_ticker4(ticker):
    """'7203.T' -> '7203'"""
    return ticker.split('.')[0]


def main():
    if not os.path.exists(SCAN_PATH):
        print(f'{SCAN_PATH} が見つかりません。')
        return

    with open(SCAN_PATH, encoding='utf-8') as f:
        data = json.load(f)
    results = data.get('results', {})
    print(f'# data/latest_scan.json 全{len(results)}銘柄を読み込みました（生成日時: {data.get("generated_at_utc")}）')

    long_t = [t for t, r in results.items() if r.get('signal') == 'LONG']
    short_t = [t for t, r in results.items() if r.get('signal') == 'SHORT']
    neutral_t = [t for t, r in results.items() if r.get('signal') == 'NEUTRAL']
    print(f'# 内訳: LONG {len(long_t)} / SHORT {len(short_t)} / NEUTRAL {len(neutral_t)}')

    selected = list(long_t)  # LONGは全件採用
    n_short = min(len(short_t), max(len(long_t), 50))
    selected += random.sample(short_t, n_short)

    remaining = max(0, TARGET_TOTAL - len(selected))
    n_neutral = min(len(neutral_t), remaining)
    selected += random.sample(neutral_t, n_neutral)

    tickers4 = [to_ticker4(t) for t in selected]
    print(f'# 選定結果: LONG全{len(long_t)} + SHORTから{n_short} + NEUTRALから{n_neutral} = 合計{len(tickers4)}銘柄')
    print()
    print(','.join(tickers4))


if __name__ == '__main__':
    main()
