#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analyze_long_filter_search.py — LONGトレードに追加の絞り込み条件をかけた場合、
勝率とペイオフレシオを同時に改善できる組み合わせが無いかを探索する分析専用
スクリプト（本番コード・trackingロジックには一切手を加えない。
analyze_entry_timing.pyと同じ位置づけ）。

【未来データ混入について】
fundamental_snapshot（Phase2レンズ用のROE等）はキャッシュ内で最新の開示を常に
使う実装のため、過去日時点では未来の情報が混入する（2026-09-11に確認済みの
既知の制約）。本スクリプトはこれを踏まえ、フィルタ条件には一切
fundamental_snapshotを使わない。score・per_pbr・dividend_yield・tech_snapshot・
patterns・entry_timingは、backtest.py側で時点整合済みに計算されるものだけを使う。

【やること】
backtest_trade_log.jsonのLONGエントリーそれぞれについて、エントリー時点の
analyze_ticker()を再計算し、score・RSI・MACD・MA・パターン検出等の条件で
絞り込んだ場合の勝率・ペイオフレシオ・期待値・件数を比較する
（ATR倍率5.0倍＝現行LONGプライマリを基準に見る）。

使い方:
    python analyze_long_filter_search.py
環境変数:
    LONG_FILTER_MIN_SAMPLES（既定150）… これ未満の件数の条件は「サンプル不足」として除外
"""

import json
import os

import pandas as pd

from collector import analyze_ticker
import tracking
from backtest import (
    load_bars_df, load_fins_timeline, fundamentals_as_of, build_fund,
    load_universe, BACKTEST_LOOKBACK_ROWS, BACKTEST_TRADE_LOG_PATH,
)

MIN_SAMPLES = int(os.getenv('LONG_FILTER_MIN_SAMPLES', '150'))
PRIMARY_KEY = tracking._variant_key(tracking.ATR_MULTIPLIER_BY_SIGNAL['LONG'])


def _stats(trades):
    s = tracking._stats_for(trades)
    if s['win_rate'] is not None and s['avg_win_pct'] is not None and s['avg_loss_pct'] is not None:
        wr = s['win_rate'] / 100
        s['expectancy_pct'] = round(wr * s['avg_win_pct'] + (1 - wr) * s['avg_loss_pct'], 3)
    else:
        s['expectancy_pct'] = None
    return s


def build_records():
    trade_log = tracking.load_trade_log(path=BACKTEST_TRADE_LOG_PATH)
    long_entries = [p for p in trade_log if p.get('signal') == 'LONG']
    print(f'LONGエントリー総数: {len(long_entries)}件。エントリー時点の指標を再計算します...')

    universe = {u['ticker4']: u.get('name') for u in load_universe()}
    bars_cache, fins_cache = {}, {}
    records = []
    excluded = 0

    for p in long_entries:
        ticker4 = p['ticker']
        primary = (p.get('variants') or {}).get(PRIMARY_KEY) or {}
        if primary.get('status') != 'closed':
            continue  # 未決済は損益未確定のため除外

        if ticker4 not in bars_cache:
            bars_cache[ticker4] = load_bars_df(ticker4)
        bars = bars_cache[ticker4]
        if bars is None:
            excluded += 1
            continue

        edate = pd.Timestamp(p['entry_date'])
        if edate not in bars.index:
            excluded += 1
            continue
        df_upto = bars.loc[:edate].tail(BACKTEST_LOOKBACK_ROWS)

        if ticker4 not in fins_cache:
            fins_cache[ticker4] = load_fins_timeline(ticker4)
        eps, bps, div_ann = fundamentals_as_of(fins_cache[ticker4], edate)
        close_price = float(df_upto['Close'].iloc[-1])
        fund = build_fund(universe.get(ticker4, ticker4), close_price, eps, bps, div_ann)

        r = analyze_ticker(ticker4, fund, df_upto)
        tech = r.get('tech_snapshot') or {}
        ma = tech.get('ma') or {}
        macd = tech.get('macd') or {}
        patterns = r.get('patterns') or []
        bullish_detected = [pt for pt in patterns if pt.get('detected') and pt.get('direction') == 'bullish']
        best_bullish_acc = max((pt.get('reference_accuracy') or 0) for pt in bullish_detected) if bullish_detected else 0

        atr_ratio = None
        last_close = tech.get('last_close')
        atr = tech.get('atr')
        if last_close and atr:
            atr_ratio = atr / last_close

        records.append({
            'return_pct': primary['return_pct'],
            'score': r.get('score'),
            'entry_timing': r.get('entry_timing'),
            'rsi': tech.get('rsi'),
            'ma_trend': ma.get('trend'),
            'golden_cross_recent': bool(ma.get('golden_cross_recent')),
            'macd_bullish_cross': bool(macd.get('bullish_cross_recent')),
            'macd_hist_positive': (macd.get('hist') or 0) > 0,
            'bullish_pattern': bool(bullish_detected),
            'best_bullish_acc': best_bullish_acc,
            'atr_ratio': atr_ratio,
            'per_pbr': r.get('per_pbr'),
            'dividend_yield': r.get('dividend_yield'),
        })

    print(f'再現できず除外: {excluded}件。分析対象: {len(records)}件')
    return records


def eval_filter(records, name, predicate):
    trades = [{'return_pct': rec['return_pct']} for rec in records if predicate(rec)]
    if len(trades) < MIN_SAMPLES:
        return None
    s = _stats(trades)
    s['name'] = name
    s['n'] = len(trades)
    return s


def main():
    records = build_records()
    if not records:
        print('分析対象データが無いため終了します。')
        return 1

    baseline = _stats([{'return_pct': rec['return_pct']} for rec in records])
    print(f"\n=== ベースライン（LONG全体・{PRIMARY_KEY}倍） ===")
    print(f"件数={len(records)} 勝率={baseline['win_rate']}% ペイオフ={baseline['payoff_ratio']} "
          f"期待値={baseline['expectancy_pct']}%")

    filters = {
        'entry_timing=good': lambda r: r['entry_timing'] == 'good',
        'entry_timing=wait': lambda r: r['entry_timing'] == 'wait',
        'score>=70': lambda r: (r['score'] or 0) >= 70,
        'score>=80': lambda r: (r['score'] or 0) >= 80,
        'score>=85': lambda r: (r['score'] or 0) >= 85,
        'score>=90': lambda r: (r['score'] or 0) >= 90,
        'ma_trend=bullish': lambda r: r['ma_trend'] == 'bullish',
        'golden_cross_recent': lambda r: r['golden_cross_recent'],
        'macd_bullish_cross': lambda r: r['macd_bullish_cross'],
        'macd_hist_positive': lambda r: r['macd_hist_positive'],
        'bullish_pattern_detected': lambda r: r['bullish_pattern'],
        'no_bullish_pattern': lambda r: not r['bullish_pattern'],
        'rsi<30(oversold)': lambda r: r['rsi'] is not None and r['rsi'] < 30,
        'rsi_30_50': lambda r: r['rsi'] is not None and 30 <= r['rsi'] < 50,
        'rsi_50_70': lambda r: r['rsi'] is not None and 50 <= r['rsi'] <= 70,
        'rsi>70(overbought)': lambda r: r['rsi'] is not None and r['rsi'] > 70,
        'atr_ratio<0.02': lambda r: r['atr_ratio'] is not None and r['atr_ratio'] < 0.02,
        'atr_ratio_0.02_0.035': lambda r: r['atr_ratio'] is not None and 0.02 <= r['atr_ratio'] <= 0.035,
        'atr_ratio>0.035': lambda r: r['atr_ratio'] is not None and r['atr_ratio'] > 0.035,
        # 組み合わせ
        'ma_bullish & golden_cross': lambda r: r['ma_trend'] == 'bullish' and r['golden_cross_recent'],
        'ma_bullish & macd_bullish_cross': lambda r: r['ma_trend'] == 'bullish' and r['macd_bullish_cross'],
        'score>=80 & rsi_50_70': lambda r: (r['score'] or 0) >= 80 and r['rsi'] is not None and 50 <= r['rsi'] <= 70,
        'score>=80 & golden_cross': lambda r: (r['score'] or 0) >= 80 and r['golden_cross_recent'],
        'entry_timing=good & score>=80': lambda r: r['entry_timing'] == 'good' and (r['score'] or 0) >= 80,
        'entry_timing=good & rsi_50_70': lambda r: r['entry_timing'] == 'good' and r['rsi'] is not None and 50 <= r['rsi'] <= 70,
        'bullish_pattern & ma_bullish': lambda r: r['bullish_pattern'] and r['ma_trend'] == 'bullish',
        'bullish_pattern & score>=70': lambda r: r['bullish_pattern'] and (r['score'] or 0) >= 70,
    }

    results = []
    for name, pred in filters.items():
        s = eval_filter(records, name, pred)
        if s is not None:
            results.append(s)

    results.sort(key=lambda s: (s['win_rate'] or 0), reverse=True)

    print(f"\n=== 全条件（n>={MIN_SAMPLES}件のみ表示） ===")
    print(f"{'条件':<32} {'件数':>6} {'勝率':>7} {'ペイオフ':>8} {'期待値':>8}")
    improved = []
    for s in results:
        mark = ''
        if (s['win_rate'] or 0) > (baseline['win_rate'] or 0) and (s['payoff_ratio'] or 0) > (baseline['payoff_ratio'] or 0):
            mark = ' ★両方改善'
            improved.append(s)
        print(f"{s['name']:<32} {s['n']:>6} {s['win_rate']:>6}% {s['payoff_ratio']:>8} {s['expectancy_pct']:>7}%{mark}")

    out_path = 'data/backtest_out/long_filter_search.json'
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump({'baseline': baseline, 'results': results, 'min_samples': MIN_SAMPLES}, f, ensure_ascii=False, indent=2)
    print(f'\n結果を保存しました: {out_path}')
    print(f"\n勝率・ペイオフ両方がベースラインを上回った条件: {len(improved)}件")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
