#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
capital_simulation.py — 500万円元手・資金配分パターン別の売買シミュレーション（先読みなし）。

evaluate_strategy.py と同じ特徴量抽出・シグナル判定・トレーリングストップのロジックを
そのまま再利用し（判定ロジックの二重実装はしない）、本番の実際の設定
（LONGスクリーニング：複合スコア75点以上・上位5・ATR×3.0倍、
  SHORTスクリーニング：PER×PBR上位10・ATR×1.8倍。tracking.ATR_MULTIPLIER_BY_SIGNAL/
  README.md「エントリー・トレーリングストップ」参照）でトレード列を作ったうえで、
capital_tracker.py と同じ考え方（equity比率での複利・投入上限）で、資金配分パターン別に
口座残高（簿価ベース：保有中ポジションは投入額のまま、決済時に確定損益を反映）を積み上げる。

酒田五法の精度は、evaluate_strategy.py のようにバックテスト内で前半期間だけから
測るのではなく、本番のsakata.pyが実際に使っている固定値（sakata.REFERENCE_ACCURACY、
sakata_eval_final.jsonから一度だけ実測・反映済み）をそのまま使う。これにより、
複合スコアの計算が本番のclassify()と完全に一致する（バックテスト用の評価目的では
「先読みなしで測る」ことが目的だが、こちらは「本番設定をそのまま過去に当てはめたら
どうなっていたか」が目的のため、本番と同じ固定値を使うほうが適切）。

【2026-09-24: 旧capital_simulation.jsonからの修正点】
旧版は本番の実際の値と異なるATR×5.0倍という誤った前提（capital_tracker.pyのコメントの
誤記と同じ原因）で計算されていた。本番の実際の値（LONG=ATR×3.0, SHORT=ATR×1.8）に
合わせて作り直した。

【実行方法】
  python capital_simulation.py                    # 全銘柄（時間がかかる。100銘柄あたり約40秒）
  python capital_simulation.py --max-tickers 100  # 動作確認用
出力: data/backtest_out/capital_simulation.json / _curves.json / _patterns.json
"""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from multiprocessing import Pool

import sakata
from evaluate_sakata import load_eval_universe
from evaluate_strategy import LONG_TOP_N, SHORT_TOP_N, classify, extract_ticker, simulate

LONG_SCORE_THRESHOLD = 75  # 本番のLONG_ENTRY_SCORE_THRESHOLD（README.md参照）
LONG_ATR = 3.0   # 本番tracking.ATR_MULTIPLIER_BY_SIGNAL['LONG']
SHORT_ATR = 1.8  # 本番tracking.ATR_MULTIPLIER_BY_SIGNAL['SHORT']
INITIAL_CAPITAL = 5_000_000


# ---------------------------------------------------------------------------
# 1. 特徴量抽出・シグナル判定・トレード列の構築（本番の実際の選定ルールをそのまま適用）
# ---------------------------------------------------------------------------

def build_trades(max_tickers=0, workers=None):
    universe = sorted(load_eval_universe(max_tickers))
    n_delisted = sum(1 for _, d in universe if d)
    workers = workers or max(1, (os.cpu_count() or 2) - 1)
    print(f'[capital_sim] 特徴量の計算: {len(universe)}銘柄（うち上場廃止 {n_delisted}） / workers={workers}',
          flush=True)

    data = []
    with Pool(workers) as pool:
        for k, res in enumerate(pool.imap(extract_ticker, universe, chunksize=2)):
            if res:
                data.append(res)
            if (k + 1) % 500 == 0:
                print(f'[capital_sim] {k + 1}/{len(universe)} 銘柄完了', flush=True)
    data.sort(key=lambda td: td['t'])

    by_date_long = defaultdict(list)   # date -> [(score, ticker, i, td_index)]
    by_date_short = defaultdict(list)  # date -> [(per_pbr, ticker, i, td_index)]
    excluded_days = 0
    for ti, td in enumerate(data):
        for row in td['rows']:
            i = row[0]
            if not row[6]:
                excluded_days += 1
                continue  # 取引できない（売買代金不足・仕手株化）日はシグナルを出さない
            d = td['dates'][i]
            # 本番と同じ固定のsakata精度テーブルを使う（in-sample測定はしない）
            signal, score = classify(row, sakata.REFERENCE_ACCURACY)
            if signal == 'LONG':
                by_date_long[d].append((score if score is not None else -1, td['t'], i, ti))
            elif signal == 'SHORT':
                by_date_short[d].append((row[2] if row[2] is not None else 0, td['t'], i, ti))

    long_cands = defaultdict(list)
    for d in sorted(by_date_long):
        picked = sorted([x for x in by_date_long[d] if x[0] >= LONG_SCORE_THRESHOLD],
                        key=lambda x: (-x[0], x[1]))[:LONG_TOP_N]
        for _, _, i, ti in picked:
            long_cands[ti].append(i)

    short_cands = defaultdict(list)
    for d in sorted(by_date_short):
        for _, _, i, ti in sorted(by_date_short[d], key=lambda x: (-x[0], x[1]))[:SHORT_TOP_N]:
            short_cands[ti].append(i)

    long_trades = []
    for ti in sorted(long_cands):
        for t in simulate(data[ti], sorted(set(long_cands[ti])), 'LONG', LONG_ATR):
            long_trades.append({**t, 'ticker': data[ti]['t'], 'signal': 'LONG'})

    short_trades = []
    for ti in sorted(short_cands):
        for t in simulate(data[ti], sorted(set(short_cands[ti])), 'SHORT', SHORT_ATR):
            short_trades.append({**t, 'ticker': data[ti]['t'], 'signal': 'SHORT'})

    all_dates = sorted({d for td in data for d in td['dates'][:]})
    period = [all_dates[0], all_dates[-1]]
    print(f'[capital_sim] 期間 {period[0]}〜{period[1]} / LONG候補 {len(long_trades)}件 '
          f'/ SHORT候補 {len(short_trades)}件 / 除外銘柄×日 {excluded_days:,}', flush=True)
    return long_trades, short_trades, all_dates


# ---------------------------------------------------------------------------
# 2. 資金配分シナリオ（capital_tracker.pyと同じ考え方：equity比率での複利・投入上限）
# ---------------------------------------------------------------------------

def run_scenario(name, trades, all_dates, initial_capital=INITIAL_CAPITAL,
                  per_trade_fraction=0.10, max_deployed_fraction=0.80,
                  fixed_size=None, max_positions_by_signal=None):
    """
    trades: [{'d','exit_d','ret','ticker','signal','open_end','delisted'}, ...]
    fixed_size: Noneならequityのper_trade_fraction、数値なら固定額（複利なし）
    max_positions_by_signal: {'LONG': n, 'SHORT': n} でシグナル別の同時保有上限（ハード件数）。
        Noneなら max_deployed_fraction（equityに対する投入比率の上限）で判定する
        （capital_tracker.pyの実際の仕組みと同じ）。
    """
    cash = initial_capital
    open_positions = []
    closed = []
    skipped_count = 0
    entries_by_date = defaultdict(list)
    for t in trades:
        entries_by_date[t['d']].append(t)

    equity_curve = []
    relevant_dates = [d for d in all_dates if d >= trades[0]['d']] if trades else []

    for date in relevant_dates:
        # 1) 今日決済されるものを確定させる
        still_open = []
        for pos in open_positions:
            if pos['exit_d'] == date:
                proceeds = pos['invested'] * (1 + pos['ret'] / 100)
                pnl = proceeds - pos['invested']
                cash += proceeds
                closed.append({'ticker': pos['ticker'], 'signal': pos['signal'],
                              'entry_date': pos['entry_date'], 'close_date': date,
                              'invested': round(pos['invested'], 0), 'pnl': round(pnl, 0),
                              'return_pct': pos['ret'], 'open_end': pos['open_end']})
            else:
                still_open.append(pos)
        open_positions = still_open

        # 2) 今日の新規候補（銘柄コード順で決定的に処理）
        for t in sorted(entries_by_date.get(date, []), key=lambda x: x['ticker']):
            invested_total = sum(p['invested'] for p in open_positions)
            equity = cash + invested_total
            position_size = fixed_size if fixed_size is not None else equity * per_trade_fraction

            if max_positions_by_signal is not None:
                cap = max_positions_by_signal.get(t['signal'], 0)
                n_same_signal = sum(1 for p in open_positions if p['signal'] == t['signal'])
                room_ok = n_same_signal < cap
            else:
                room = equity * max_deployed_fraction - invested_total
                room_ok = position_size <= room

            if not room_ok or position_size > cash or position_size <= 0:
                skipped_count += 1
                continue

            cash -= position_size
            open_positions.append({'ticker': t['ticker'], 'signal': t['signal'], 'entry_date': date,
                                    'exit_d': t['exit_d'], 'ret': t['ret'], 'invested': position_size,
                                    'open_end': t['open_end']})

        invested_total = sum(p['invested'] for p in open_positions)
        equity_curve.append({'date': date, 'equity': round(cash + invested_total, 0)})

    final_equity = equity_curve[-1]['equity'] if equity_curve else initial_capital
    peak = initial_capital
    max_dd = 0.0
    max_dd_date = None
    for row in equity_curve:
        peak = max(peak, row['equity'])
        dd = (row['equity'] - peak) / peak * 100 if peak else 0.0
        if dd < max_dd:
            max_dd = dd
            max_dd_date = row['date']

    years = None
    if len(relevant_dates) >= 2:
        from datetime import date as _date
        d0 = _date.fromisoformat(relevant_dates[0])
        d1 = _date.fromisoformat(relevant_dates[-1])
        years = max((d1 - d0).days / 365.25, 1 / 365.25)
    cagr = (((final_equity / initial_capital) ** (1 / years) - 1) * 100) if years and final_equity > 0 else None

    by_signal = {}
    for sig in ('LONG', 'SHORT'):
        sig_closed = [c for c in closed if c['signal'] == sig]
        if not sig_closed:
            continue
        wins = [c for c in sig_closed if c['return_pct'] > 0]
        by_signal[sig] = {
            'closed_count': len(sig_closed),
            'win_rate_pct': round(len(wins) / len(sig_closed) * 100, 1),
            'total_pnl': round(sum(c['pnl'] for c in sig_closed), 0),
        }

    wins_all = [c for c in closed if c['return_pct'] > 0]
    return {
        'name': name,
        'entered_count': len(closed) + len(open_positions),
        'total_signals': len(trades),
        'skipped_count': skipped_count,
        'realized_trade_count': len(closed),
        'win_rate_pct': round(len(wins_all) / len(closed) * 100, 1) if closed else None,
        'total_realized_pnl': round(sum(c['pnl'] for c in closed), 0),
        'open_positions_at_end': len(open_positions),
        'final_equity_book': round(final_equity, 0),
        'pnl': round(final_equity - initial_capital, 0),
        'return_pct': round((final_equity / initial_capital - 1) * 100, 2),
        'max_drawdown_pct': round(max_dd, 2),
        'max_drawdown_date': max_dd_date,
        'cagr_pct': round(cagr, 2) if cagr is not None else None,
        'by_signal': by_signal,
    }, equity_curve


# ---------------------------------------------------------------------------
# 3. メイン
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--max-tickers', type=int, default=0)
    ap.add_argument('--workers', type=int, default=0)
    ap.add_argument('--out', default='data/backtest_out/capital_simulation')
    args = ap.parse_args()

    long_trades, short_trades, all_dates = build_trades(args.max_tickers, args.workers or None)
    both_trades = long_trades + short_trades

    scenarios_def = [
        ('ベースライン（10%複利・最大8/前回と同条件）', long_trades,
         dict(per_trade_fraction=0.10, max_deployed_fraction=0.80)),
        ('パターン1（5%複利・最大16ポジション）', long_trades,
         dict(per_trade_fraction=0.05, max_deployed_fraction=0.80)),
        ('パターン2（10%複利・上限100%＝最大10ポジション）', long_trades,
         dict(per_trade_fraction=0.10, max_deployed_fraction=1.00)),
        ('パターン3（固定50万円/トレード・複利なし・最大8ポジション）', long_trades,
         dict(fixed_size=500_000, max_positions_by_signal={'LONG': 8, 'SHORT': 8})),
        ('パターン4（信用取引：LONG+SHORT・LONG最大6/SHORT最大3）', both_trades,
         dict(per_trade_fraction=0.10, max_positions_by_signal={'LONG': 6, 'SHORT': 3})),
    ]

    summaries = []
    curves = {}
    for name, trades, kwargs in scenarios_def:
        summary, curve = run_scenario(name, trades, all_dates, **kwargs)
        summaries.append(summary)
        curves[name] = curve
        print(f'[capital_sim] {name}: 参入{summary["entered_count"]}件・決済{summary["realized_trade_count"]}件・'
              f'勝率{summary["win_rate_pct"]}%・最終簿価{summary["final_equity_book"]:,.0f}円'
              f'（{summary["return_pct"]:+.2f}%）・最大DD{summary["max_drawdown_pct"]:.2f}%', flush=True)

    baseline_summary, _ = run_scenario('ベースライン（詳細出力用）', long_trades, all_dates,
                                        per_trade_fraction=0.10, max_deployed_fraction=0.80)

    detail = {
        'assumptions': {
            'initial_capital': INITIAL_CAPITAL,
            'per_trade_fraction': 0.10,
            'max_deployed_fraction': 0.80,
            'signal': 'LONG_only_spot',
            'atr_multiplier_long': LONG_ATR,
            'atr_multiplier_short': SHORT_ATR,
            'long_score_threshold': LONG_SCORE_THRESHOLD,
            'long_top_n': LONG_TOP_N,
            'short_top_n': SHORT_TOP_N,
            'note': '2026-09-24: 旧版のATR×5.0倍という誤った前提を、本番の実際の値'
                    '（LONG=ATR×3.0, SHORT=ATR×1.8）に修正して作り直した。',
        },
        'period': [all_dates[0], all_dates[-1]] if all_dates else None,
        'summary': baseline_summary,
    }

    os.makedirs(os.path.dirname(args.out) or '.', exist_ok=True)
    with open(args.out + '.json', 'w', encoding='utf-8') as f:
        json.dump(detail, f, ensure_ascii=False, indent=2)
    with open(args.out + '_patterns.json', 'w', encoding='utf-8') as f:
        json.dump({'initial_capital': INITIAL_CAPITAL, 'scenarios': summaries}, f, ensure_ascii=False, indent=2)
    with open(args.out + '_curves.json', 'w', encoding='utf-8') as f:
        json.dump(curves, f, ensure_ascii=False, indent=2)
    print(f'[capital_sim] 完了: {args.out}.json / _patterns.json / _curves.json', flush=True)


if __name__ == '__main__':
    main()
