#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analyze_atr_by_sector.py — ATR倍率の最適値が業種（33業種コード）ごとに違うかを、
backtest.py の結果から集計する分析専用スクリプト（本番コード・trackingロジックには
一切手を加えない。analyze_entry_timing.py と同じ位置づけ）。

【前提・既知の制約】
この分析はトレードのエントリー時点で記録された 'sector_code'（tracking.py
open_new_positions()が2026-09-07に追加）が無ければ機能しない。以下の場合は
sector_codeがNoneのままで、業種別集計の対象外になる：
  - backfill_jquants.py実行時にdata/jquants_cache/_universe.jsonへ業種コードが
    書き込まれていない（universe.pyのJPX一覧取得がネットワーク制限で失敗した場合、
    または本スクリプト追加より前に生成された古い_universe.jsonを使っている場合）
  - それ以前（2026-09-07より前）にエントリーした古いトレード
そのため、意味のある業種別集計を得るには、
  1. python backfill_jquants.py を再実行して_universe.jsonに業種コードを反映
  2. python backtest.py を再実行してtrade_logにsector_codeを記録
の2ステップが必要（いずれもネットワークアクセスが要る。開発環境では未実施）。

【集計方針】
業種ごとにサンプル数が少なすぎると統計として意味が無いため、
MIN_SAMPLES_PER_SECTOR件未満の（業種, シグナル, 倍率）の組み合わせは
表示から除外する。

使い方:
    python analyze_atr_by_sector.py
"""

import json
import os

import tracking
from backtest import BACKTEST_TRADE_LOG_PATH

MIN_SAMPLES_PER_SECTOR = int(os.getenv('ATR_SECTOR_MIN_SAMPLES', '15'))


def _expectancy(s):
    if s['win_rate'] is None or s['avg_win_pct'] is None or s['avg_loss_pct'] is None:
        return None
    wr = s['win_rate'] / 100
    return round(wr * s['avg_win_pct'] + (1 - wr) * s['avg_loss_pct'], 3)


def _closed_variant_trades_by_sector(trade_log, variant_key, sector_code, signal=None):
    out = []
    for p in trade_log:
        if p.get('sector_code') != sector_code:
            continue
        if signal is not None and p.get('signal') != signal:
            continue
        v = (p.get('variants') or {}).get(variant_key)
        if v and v.get('status') == 'closed':
            out.append(v)
    return out


def main():
    if not os.path.exists(BACKTEST_TRADE_LOG_PATH):
        print(f'[analyze_atr_by_sector] {BACKTEST_TRADE_LOG_PATH} が見つかりません。'
              '先に backtest.py を実行してください。')
        return 1

    with open(BACKTEST_TRADE_LOG_PATH, encoding='utf-8') as f:
        trade_log = json.load(f)

    sector_codes = sorted({p.get('sector_code') for p in trade_log if p.get('sector_code')})
    with_sector = sum(1 for p in trade_log if p.get('sector_code'))
    print(f'[analyze_atr_by_sector] トレード総数{len(trade_log)}件中、業種コード付き{with_sector}件'
          f'（{len(sector_codes)}業種）')

    if not sector_codes:
        print('[analyze_atr_by_sector] 業種コード付きのトレードが1件もありません。')
        print('  → backfill_jquants.py → backtest.py の順で再実行し、'
              '業種コードをtrade_logに反映させてから再度お試しください。')
        return 1

    sector_names = {}
    for p in trade_log:
        if p.get('sector_code') and p.get('sector_code') not in sector_names:
            sector_names[p['sector_code']] = (p.get('sector_name') or p['sector_code'])

    results = {}
    for sector_code in sector_codes:
        sector_result = {'sector_name': sector_names.get(sector_code, sector_code), 'signals': {}}
        for signal in ('LONG', 'SHORT'):
            variant_stats = {}
            for m in tracking.ATR_MULTIPLIER_VARIANTS:
                key = tracking._variant_key(m)
                trades = _closed_variant_trades_by_sector(trade_log, key, sector_code, signal)
                if len(trades) < MIN_SAMPLES_PER_SECTOR:
                    continue
                s = tracking._stats_for(trades)
                s['expectancy'] = _expectancy(s)
                variant_stats[key] = s
            if variant_stats:
                best_key = max(variant_stats, key=lambda k: (variant_stats[k]['expectancy'] or -999))
                sector_result['signals'][signal] = {
                    'variants': variant_stats,
                    'best_multiplier': best_key,
                    'best_expectancy': variant_stats[best_key]['expectancy'],
                }
        if sector_result['signals']:
            results[sector_code] = sector_result

    out_path = os.path.join(os.path.dirname(BACKTEST_TRADE_LOG_PATH), 'atr_by_sector.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f'[analyze_atr_by_sector] {len(results)}業種分の結果を書き込みました: {out_path}')
    for sector_code, r in sorted(results.items(), key=lambda kv: kv[1]['sector_name']):
        print(f"\n{r['sector_name']}（{sector_code}）")
        for signal, sig_result in r['signals'].items():
            print(f"  {signal}: 最適倍率={sig_result['best_multiplier']} "
                  f"期待値={sig_result['best_expectancy']}%")

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
