#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analyze_entry_timing.py — LONGエントリーの「entry_timing（good/wait）」別に成績を集計する
分析専用スクリプト（本番コード・trackingロジックには一切手を加えない）。

【背景】
collector.analyze_ticker()はLONGシグナルに対して entry_timing='good'（テクニカルスコア0.5以上
かつ強い弱気パターンなし）/'wait' を計算しているが、tracking.open_new_positions()はこれを
一切参照せず、score順の上位10銘柄を無条件に仮想エントリーしている（本番・バックテスト共通の
挙動）。これが「LONGだけペイオフレシオが1を割る」ことの一因かどうかを、後付けで確認する。

【やること】
backtest.py が作った data/backtest_out/backtest_trade_log.json の中からLONGエントリーを
すべて取り出し、それぞれの「エントリー時点」で analyze_ticker を再実行して entry_timing を
再計算し、good/waitの2グループに分けて勝率・ペイオフレシオ・期待値を比較する。
tracking.py・collector.py本体は変更しないため、本番の挙動には一切影響しない。

【実行方法】（backfill_jquants.py・backtest.py を実行済みであること）
  python analyze_entry_timing.py
"""

import json
import os

from collector import analyze_ticker
import tracking
from backtest import load_bars_df, fundamentals_as_of, build_fund, load_universe, BACKTEST_LOOKBACK_ROWS, BACKTEST_TRADE_LOG_PATH


def _stats_with_expectancy(trades):
    """tracking._stats_for()相当に「1トレード期待値」を追加したもの。"""
    s = tracking._stats_for(trades)
    if s['win_rate'] is not None and s['avg_win_pct'] is not None and s['avg_loss_pct'] is not None:
        wr = s['win_rate'] / 100
        s['expectancy_pct'] = round(wr * s['avg_win_pct'] + (1 - wr) * s['avg_loss_pct'], 3)
    else:
        s['expectancy_pct'] = None
    return s


def main():
    if not os.path.exists(BACKTEST_TRADE_LOG_PATH):
        print(f'{BACKTEST_TRADE_LOG_PATH} が見つかりません。先に backtest.py を実行してください。')
        return

    trade_log = tracking.load_trade_log(path=BACKTEST_TRADE_LOG_PATH)
    long_entries = [p for p in trade_log if p.get('signal') == 'LONG']
    print(f'LONGエントリー総数: {len(long_entries)}件。エントリー時点のentry_timingを再計算します...')

    universe = {u['ticker4']: u.get('name') for u in load_universe()}
    bars_cache = {}
    fins_cache = {}

    good_trades, wait_trades, unknown = [], [], 0

    for p in long_entries:
        ticker4 = p['ticker']
        entry_date = p['entry_date']

        if ticker4 not in bars_cache:
            bars_cache[ticker4] = load_bars_df(ticker4)
        bars = bars_cache[ticker4]
        if bars is None:
            unknown += 1
            continue

        import pandas as pd
        edate = pd.Timestamp(entry_date)
        if edate not in bars.index:
            unknown += 1
            continue
        df_upto = bars.loc[:edate].tail(BACKTEST_LOOKBACK_ROWS)

        if ticker4 not in fins_cache:
            from backtest import load_fins_timeline
            fins_cache[ticker4] = load_fins_timeline(ticker4)
        eps, bps, div_ann = fundamentals_as_of(fins_cache[ticker4], edate)
        close_price = float(df_upto['Close'].iloc[-1])
        fund = build_fund(universe.get(ticker4, ticker4), close_price, eps, bps, div_ann)

        r = analyze_ticker(ticker4, fund, df_upto)
        timing = r.get('entry_timing')

        primary = (p.get('variants') or {}).get(tracking.PRIMARY_VARIANT_KEY_BY_SIGNAL['LONG']) or {}
        if primary.get('status') != 'closed':
            continue  # 決済済みのものだけを集計対象にする（保有中は損益未確定のため除外）

        if timing == 'good':
            good_trades.append(primary)
        elif timing == 'wait':
            wait_trades.append(primary)
        else:
            unknown += 1

    print(f'\n再現できず集計から除外: {unknown}件（データ欠損等）')
    print(f'\n=== entry_timing=good（{len(good_trades)}件） ===')
    print(_stats_with_expectancy(good_trades))
    print(f'\n=== entry_timing=wait（{len(wait_trades)}件） ===')
    print(_stats_with_expectancy(wait_trades))

    out_path = os.path.join(os.path.dirname(BACKTEST_TRADE_LOG_PATH), 'entry_timing_analysis.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump({
            'good': {'count': len(good_trades), 'stats': _stats_with_expectancy(good_trades)},
            'wait': {'count': len(wait_trades), 'stats': _stats_with_expectancy(wait_trades)},
            'excluded': unknown,
        }, f, ensure_ascii=False, indent=2)
    print(f'\n結果を保存しました: {out_path}')


if __name__ == '__main__':
    main()
