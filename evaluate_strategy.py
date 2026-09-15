#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
evaluate_strategy.py — スクリーニング（LONG/SHORT判定・複合スコア）・酒田五法・出来高急増シグナルを、
「未来の情報を一切使わない」条件で売買シミュレーションし、トレーリングストップのATR倍率ごとの
勝率・ペイオフレシオ・期待値を、前半期間（選ぶ期間）と後半期間（確かめる期間）に分けて検証する
分析専用スクリプト（本番コード・trackingロジックには一切手を加えない）。

【先読み（未来データ混入）を防ぐためのルール】
1. シグナルは t日の引けまでの日足だけで計算する（本番と同じ判定式。指標は fast_features.py で本番関数と
   一致を確認済みの高速版を使う）。
2. エントリーは t+1日の始値。シグナルと同じ t日の終値では買わない
   （本番はシグナルを翌朝8時に投稿するため、t日の終値では約定できない）。
3. 決済は、t日の終値がトレーリングストップに抵触したら t+1日の始値。
4. ファンダメンタルズ（EPS・BPS・年間配当）は、開示日（DiscDate）が t日以前の本決算開示だけを使う。
   本番の build_fundamental_snapshot()（常に最新の開示を使う）はシグナル計算に使わない。
5. 酒田五法の実測精度（スコアの重み）は、前半期間の検出だけで測った値を使い、前半期間の成績で
   パラメータを選ぶ。後半期間はその値をそのまま当てはめて確かめるだけ。
   前半期間の検出のうち、20営業日後が後半期間にはみ出すものは精度の計測から除外する。
6. 株式分割：チャート・ATR・シグナルは分割調整済み株価で計算する。PER・PBR・配当利回りは
   「その日の実際の株価（生の終値）」と「開示時点のEPS・BPS・配当を、開示日の翌日〜その日までに
   起きた分割だけで換算した値」で計算する（調整済み株価は将来の分割で過去が書き換わるため使わない）。
7. 上場廃止：期間中に上場廃止になった銘柄（backfill_delisted.py で取得）も対象に含め、保有中に
   上場廃止になった場合は最終取引日の終値で決済する（TOBプレミアム・破綻とも成績に反映）。
8. 取引できない銘柄の除外：シグナル日時点で universe_filters.tradeable_flags() を満たさない銘柄
   （売買代金不足・売買不成立日が多い・仕手株化の兆候）にはエントリーしない。株価の上限は考慮しない。

【再現性】
銘柄はコード順に処理し、同点の並びも（スコア, 銘柄コード, 日付）で決める。乱数は使わない。
出力JSONの meta にコード版・データ指紋・全パラメータを記録する。

【残る限界（正直な注意点）】
- 上場廃止銘柄は、Freeプランで取得できる期間内に約3か月おきの上場銘柄一覧で確認できたものに限る。
- J-Quantsの財務データに後日の訂正が反映されている場合、その分は区別できない。
- 手数料・スリッページは往復 COST_PCT（既定0.1%）で近似。空売りの貸株料・逆日歩は含まない。
- ポジションサイズや同時保有数の上限・資金制約はモデル化していない（1トレード＝1単位の成績）。

【実行方法】
  python evaluate_strategy.py                      # 全銘柄
  python evaluate_strategy.py --max-tickers 100    # 動作確認
出力: data/backtest_out/strategy_eval.json / .md
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from multiprocessing import Pool

import repro
import sakata
from backtest import build_fund, fundamentals_as_of, load_fins_timeline
from evaluate_sakata import load_adjusted_bars, load_eval_universe
from fast_features import precompute_technicals, snapshot_at, volume_surge_quiet_flags
from indicators import technical_score
from scoring import composite_score
from universe_filters import filter_params, tradeable_flags

LOOKBACK_ROWS = 220
MIN_ROWS = 80
COST_PCT = float(os.getenv('STRATEGY_COST_PCT', '0.1'))  # 往復コスト（%）
FALLBACK_STOP_PCT = 0.03
ATR_GRID = [1.0, 1.5, 1.8, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 6.0]
LONG_SCORE_THRESHOLDS = [0, 60, 65, 70, 75, 80, 85, 90]
LONG_TOP_N = 5
SHORT_TOP_N = 10
MIN_TRADES_FOR_PICK = 100
VOLUME_SURGE_RATIOS = (3.0, 2.0)


# ---------------------------------------------------------------------------
# 1. 銘柄ごとの特徴量（並列）
# ---------------------------------------------------------------------------

def split_factor_since(df_index, adj_factors, disc_date, i):
    """開示日の翌日〜i日目までに起きた分割の係数の積（1:2分割なら0.5）。"""
    f = 1.0
    j = i
    while j >= 0 and df_index[j] > disc_date:
        f *= adj_factors[j]
        j -= 1
    return f


def extract_ticker(item):
    ticker4, is_delisted = item
    loaded = load_adjusted_bars(ticker4, return_raw=True)
    if loaded is None:
        return None
    df, raw = loaded
    if len(df) < MIN_ROWS + 5:
        return None
    fins = load_fins_timeline(ticker4)
    n = len(df)
    opens = df['Open'].to_numpy(dtype=float)
    closes = df['Close'].to_numpy(dtype=float)
    raw_closes = df['RawClose'].to_numpy(dtype=float)
    adj_factors = df['AdjFactor'].to_numpy(dtype=float)
    idx = df.index

    pre = precompute_technicals(df)
    atr = pre['atr']
    tradeable, _ = tradeable_flags(raw, df, atr)
    vol_flags = {r: volume_surge_quiet_flags(df, pre, r) for r in VOLUME_SURGE_RATIOS}
    bars = sakata._Bars.from_df(df)

    rows = []
    for i in range(MIN_ROWS - 1, n):
        # ファンダメンタルズ：開示日 <= t の本決算だけ（未来の開示は使わない）
        eps, bps, div_ann = fundamentals_as_of(fins, idx[i])
        disc = max((r['date'] for r in fins if r['date'] <= idx[i]), default=None)
        raw_close = raw_closes[i] if raw_closes[i] == raw_closes[i] and raw_closes[i] > 0 else closes[i]
        if disc is not None:
            f = split_factor_since(idx, adj_factors, disc, i)
            eps = eps * f if eps is not None else None
            bps = bps * f if bps is not None else None
            div_ann = div_ann * f if div_ann is not None else None
        fund = build_fund(ticker4, raw_close, eps, bps, div_ann)
        per, pbr, div = fund['per'] or 0, fund['pbr'] or 0, fund['dividend_yield'] or 0
        per_pbr = per * pbr if per and pbr else None

        snap = snapshot_at(pre, i)
        tech_val, _ = technical_score(snap)
        lo = max(0, i - LOOKBACK_ROWS + 1)
        window = sakata._Bars(bars.o[lo:i + 1], bars.h[lo:i + 1], bars.l[lo:i + 1], bars.c[lo:i + 1])
        detected = tuple((p['name'], p['direction']) for p in sakata.detect_all_patterns(window)
                         if p['detected'] and p['direction'] in ('bullish', 'bearish'))
        rows.append((i, div, per_pbr, tech_val, snap['ma']['trend'], detected, bool(tradeable[i]),
                     tuple(bool(vol_flags[r][i]) for r in VOLUME_SURGE_RATIOS)))

    return {'t': ticker4, 'dates': [d.strftime('%Y-%m-%d') for d in idx], 'O': opens, 'C': closes,
            'ATR': atr, 'rows': rows, 'delisted': is_delisted}


# ---------------------------------------------------------------------------
# 2. 本番ロジック（collector.analyze_ticker）と同じ判定を、特徴量から再計算する
# ---------------------------------------------------------------------------

def classify(row, accuracy):
    """accuracy: {パターン名: 精度%}。analyze_ticker() の signal/score 判定と同じ式。"""
    _, div, per_pbr, tech_val, ma_trend, detected = row[:6]
    pats = [{'name': nm, 'detected': True, 'direction': d, 'reference_accuracy': accuracy.get((nm, d)),
             'note': ''} for nm, d in detected]
    sak_val, _ = sakata.sakata_score(pats)
    score, _, _ = composite_score(dividend_yield=div if div else None, per_pbr=per_pbr, eps_trend=None,
                                  technical_score_val=tech_val, sakata_score_val=sak_val, growth_value=None)
    bearish_strong = any(p['direction'] == 'bearish' and (p['reference_accuracy'] or 0) >= 70 for p in pats)
    dividend_ok = bool(div) and 3.5 <= div <= 5.8
    per_pbr_ok = per_pbr is not None and per_pbr <= 22.5
    if dividend_ok and per_pbr_ok:
        signal = 'LONG'
    elif (per_pbr is not None and per_pbr > 30) or (ma_trend == 'bearish' and bearish_strong):
        signal = 'SHORT'
    else:
        signal = 'NEUTRAL'
    return signal, score


# ---------------------------------------------------------------------------
# 3. 売買シミュレーション（1銘柄・1戦略・1倍率）
# ---------------------------------------------------------------------------

def simulate(td, cand_idx, side, m):
    """
    cand_idx: シグナル日のインデックス（昇順）。建玉中は同じ銘柄に新規エントリーしない。
    データの最終日まで決済されなかった場合：上場廃止銘柄なら最終取引日の終値で決済（'delisted'）、
    それ以外は期間末の終値で評価（'open_end'）。
    """
    O, C, A = td['O'], td['C'], td['ATR']
    n = len(C)
    trades = []
    busy_until = -1
    for i in cand_idx:
        if i < busy_until or i + 1 >= n:
            continue
        entry = O[i + 1]
        if not entry or entry <= 0:
            continue
        a0 = A[i] if A[i] == A[i] and A[i] > 0 else entry * FALLBACK_STOP_PCT
        stop = entry - a0 * m if side == 'LONG' else entry + a0 * m
        exit_price, exit_idx, still_open, delisted_exit = None, None, False, False
        for j in range(i + 1, n):
            a = A[j] if A[j] == A[j] and A[j] > 0 else C[j] * FALLBACK_STOP_PCT
            if side == 'LONG':
                stop = max(stop, C[j] - a * m)
                hit = C[j] <= stop
            else:
                stop = min(stop, C[j] + a * m)
                hit = C[j] >= stop
            if hit:
                if j + 1 < n:
                    exit_price, exit_idx = O[j + 1], j + 1
                else:
                    exit_price, exit_idx = C[j], j
                    still_open, delisted_exit = not td['delisted'], td['delisted']
                break
        if exit_price is None:
            exit_price, exit_idx = C[n - 1], n - 1
            still_open, delisted_exit = not td['delisted'], td['delisted']
        gross = (exit_price / entry - 1) if side == 'LONG' else (1 - exit_price / entry)
        trades.append({'d': td['dates'][i], 'exit_d': td['dates'][exit_idx], 'ret': gross * 100 - COST_PCT,
                       'hold': exit_idx - i, 'open_end': still_open, 'delisted': delisted_exit})
        busy_until = exit_idx
    return trades


MARKET_LEVEL = {}  # date -> 取引可能な全銘柄の等金額指数（main で daily.pkl から作る）


def market_return_pct(t):
    """トレードと同じ期間（シグナル日の終値→決済日の終値）の市場平均の上昇率（%）。LONG基準。"""
    a, b = MARKET_LEVEL.get(t['d']), MARKET_LEVEL.get(t['exit_d'])
    return (b / a - 1) * 100 if a and b else None


def stats(trades, side='LONG'):
    n = len(trades)
    if n == 0:
        return {'n': 0}
    rets = [t['ret'] for t in trades]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    avg_w = sum(wins) / len(wins) if wins else 0.0
    avg_l = sum(losses) / len(losses) if losses else 0.0
    exp = sum(rets) / n
    hold = sum(t['hold'] for t in trades) / n
    pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else None
    excess = []
    for t in trades:
        m = market_return_pct(t)
        if m is not None:
            excess.append(t['ret'] - (m if side == 'LONG' else -m))
    ex_mean = sum(excess) / len(excess) if excess else None
    return {
        'n': n,
        'excess_expectancy_pct': round(ex_mean, 3) if ex_mean is not None else None,
        'excess_win_rate_pct': round(sum(1 for x in excess if x > 0) / len(excess) * 100, 1) if excess else None,
        'excess_per_day_pct': round(ex_mean / hold, 4) if ex_mean is not None and hold else None,
        'win_rate_pct': round(len(wins) / n * 100, 1),
        'avg_win_pct': round(avg_w, 2),
        'avg_loss_pct': round(avg_l, 2),
        'payoff_ratio': round(avg_w / abs(avg_l), 2) if avg_l else None,
        'expectancy_pct': round(exp, 3),
        'profit_factor': round(pf, 2) if pf is not None else None,
        'avg_hold_days': round(hold, 1),
        'expectancy_per_day_pct': round(exp / hold, 4) if hold else None,
        'open_at_end': sum(1 for t in trades if t['open_end']),
        'delisted_exit': sum(1 for t in trades if t['delisted']),
    }


# ---------------------------------------------------------------------------
# 4. メイン
# ---------------------------------------------------------------------------

def measure_accuracy(data, split_date):
    """前半期間（t+20営業日が split_date より前に収まる・取引可能な日の検出）だけで、20日方向一致率を測る。"""
    hits = defaultdict(lambda: [0, 0])
    for td in data:
        C, O, dates = td['C'], td['O'], td['dates']
        n = len(C)
        for row in td['rows']:
            i, detected, ok = row[0], row[5], row[6]
            if not detected or not ok or i + 20 >= n or dates[i + 20] >= split_date:
                continue
            entry = O[i + 1]
            if not entry or entry <= 0:
                continue
            r = C[i + 20] / entry - 1
            for name, d in detected:
                h = hits[(name, d)]
                h[1] += 1
                h[0] += 1 if (r > 0 if d == 'bullish' else r < 0) else 0
    acc = {key: round(w / t * 100) for key, (w, t) in sorted(hits.items()) if t >= 30}
    counts = {key: t for key, (w, t) in sorted(hits.items())}
    return acc, counts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--max-tickers', type=int, default=0)
    ap.add_argument('--workers', type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument('--out', default='data/backtest_out/strategy_eval')
    args = ap.parse_args()

    universe = sorted(load_eval_universe(args.max_tickers))
    n_delisted = sum(1 for _, d in universe if d)
    print(f'[strategy] 特徴量の計算: {len(universe)}銘柄（うち上場廃止 {n_delisted}） / workers={args.workers}',
          flush=True)

    data = []
    with Pool(args.workers) as pool:
        for k, res in enumerate(pool.imap(extract_ticker, universe, chunksize=2)):
            if res:
                data.append(res)
            if (k + 1) % 500 == 0:
                print(f'[strategy] {k + 1}/{len(universe)} 銘柄完了', flush=True)
    data.sort(key=lambda td: td['t'])

    # 市場平均（取引可能な全銘柄の等金額指数）。build_panel.py が作る daily.pkl を使う
    daily_path = os.path.join('data/backtest_out', 'daily.pkl')
    if os.path.exists(daily_path):
        import pandas as pd
        from evaluate_crashes import build_index
        idx_df = build_index(pd.read_pickle(daily_path))[0]
        MARKET_LEVEL.update(idx_df['ew_level'].to_dict())
    else:
        print('[strategy] daily.pkl が無いため市場平均との比較を省略します（先に build_panel.py を実行）', flush=True)

    all_dates = sorted({d for td in data for d in td['dates'][MIN_ROWS - 1:]})
    split_date = all_dates[len(all_dates) // 2]
    print(f'[strategy] 期間 {all_dates[0]}〜{all_dates[-1]} / 後半の開始日 {split_date}', flush=True)

    acc_is, acc_counts = measure_accuracy(data, split_date)
    print(f'[strategy] 前半期間で測った酒田五法の20日方向一致率: { {f"{k[0]}|{k[1]}": v for k, v in acc_is.items()} }', flush=True)

    by_date_long = defaultdict(list)   # date -> [(score, ticker, i, td_index)]
    by_date_short = defaultdict(list)  # date -> [(per_pbr, ticker, i, td_index)]
    sakata_events = defaultdict(lambda: defaultdict(list))  # 'name|dir' -> td_index -> [i]
    volume_quiet_events = {r: defaultdict(list) for r in VOLUME_SURGE_RATIOS}
    excluded_days = 0
    for ti, td in enumerate(data):
        for row in td['rows']:
            i = row[0]
            if not row[6]:
                excluded_days += 1
                continue  # 取引できない（売買代金不足・仕手株化）日はシグナルを出さない
            d = td['dates'][i]
            signal, score = classify(row, acc_is)
            if signal == 'LONG':
                by_date_long[d].append((score if score is not None else -1, td['t'], i, ti))
            elif signal == 'SHORT':
                by_date_short[d].append((row[2] if row[2] is not None else 0, td['t'], i, ti))
            for name, direction in row[5]:
                sakata_events[f'{name}|{direction}'][ti].append(i)
                all_key = '酒田五法（全買いパターン）|bullish' if direction == 'bullish' else '酒田五法（全売りパターン）|bearish'
                sakata_events[all_key][ti].append(i)
            for k, r in enumerate(VOLUME_SURGE_RATIOS):
                if row[7][k]:
                    volume_quiet_events[r][ti].append(i)

    def split_stats(trades, side):
        first = [t for t in trades if t['d'] < split_date]
        second = [t for t in trades if t['d'] >= split_date]
        return {'前半（選ぶ期間）': stats(first, side), '後半（確かめる期間）': stats(second, side),
                '全期間': stats(trades, side)}

    params = {'atr_grid': ATR_GRID, 'long_score_thresholds': LONG_SCORE_THRESHOLDS, 'long_top_n': LONG_TOP_N,
              'short_top_n': SHORT_TOP_N, 'cost_pct_round_trip': COST_PCT, 'min_trades_for_pick': MIN_TRADES_FOR_PICK,
              'lookback_rows': LOOKBACK_ROWS, 'min_rows': MIN_ROWS, 'volume_surge_ratios': VOLUME_SURGE_RATIOS,
              'universe_filter': filter_params(), 'entry': 'next_open', 'exit': 'next_open_after_close_breach'}
    report = {'meta': repro.run_metadata([t for t, _ in universe], params,
                                         ['evaluate_strategy.py', 'sakata.py', 'fast_features.py',
                                          'universe_filters.py', 'scoring.py', 'backtest.py', 'indicators.py']),
              'period': [all_dates[0], all_dates[-1]], 'split_date': split_date, 'tickers': len(data),
              'delisted_tickers': sum(1 for td in data if td['delisted']), 'excluded_stock_days': excluded_days,
              'cost_pct_round_trip': COST_PCT,
              'sakata_accuracy_first_half': {f'{k[0]}|{k[1]}': v for k, v in acc_is.items()},
              'sakata_detections_first_half': {f'{k[0]}|{k[1]}': v for k, v in acc_counts.items()}, 'strategies': {}}

    def run_strategy(label, side, cands_by_ti):
        grid = {}
        for m in ATR_GRID:
            trades = []
            for ti in sorted(cands_by_ti):
                trades += simulate(data[ti], sorted(set(cands_by_ti[ti])), side, m)
            grid[str(m)] = split_stats(trades, side)
        first = {m: g['前半（選ぶ期間）'] for m, g in grid.items()}
        # 選定基準：前半期間の「市場平均を差し引いた期待値」が最大の倍率（長く持つだけで上昇相場に乗る効果を除く）
        eligible = [(s['excess_expectancy_pct'], -float(m), m) for m, s in first.items()
                    if s.get('n', 0) >= MIN_TRADES_FOR_PICK and s.get('excess_expectancy_pct') is not None]
        pick = max(eligible)[2] if eligible else None  # 同点なら小さい倍率を優先（決定的）
        report['strategies'][label] = {'side': side, 'atr_grid': grid, 'picked_atr_by_first_half': pick}
        if pick:
            o = grid[pick]['後半（確かめる期間）']
            print(f'[strategy] {label}: 前半で選んだATR倍率={pick} → 後半 n={o.get("n")} 勝率={o.get("win_rate_pct")}% '
                  f'PR={o.get("payoff_ratio")} 期待値={o.get("expectancy_pct")}% 超過期待値={o.get("excess_expectancy_pct")}%',
                  flush=True)
        else:
            print(f'[strategy] {label}: 前半のトレード数が{MIN_TRADES_FOR_PICK}件未満のため選定なし', flush=True)

    for thr in LONG_SCORE_THRESHOLDS:
        cands = defaultdict(list)
        for d in sorted(by_date_long):
            picked = sorted([x for x in by_date_long[d] if x[0] >= thr], key=lambda x: (-x[0], x[1]))[:LONG_TOP_N]
            for _, _, i, ti in picked:
                cands[ti].append(i)
        run_strategy(f'スクリーニングLONG（スコア{thr}点以上・上位{LONG_TOP_N}）', 'LONG', cands)

    cands = defaultdict(list)
    for d in sorted(by_date_short):
        for _, _, i, ti in sorted(by_date_short[d], key=lambda x: (-x[0], x[1]))[:SHORT_TOP_N]:
            cands[ti].append(i)
    run_strategy(f'スクリーニングSHORT（PER×PBR上位{SHORT_TOP_N}）', 'SHORT', cands)

    for key in sorted(sakata_events):
        name, direction = key.split('|')
        run_strategy(f'酒田五法：{name}（{"買い" if direction == "bullish" else "売り"}）',
                     'LONG' if direction == 'bullish' else 'SHORT', sakata_events[key])

    for r in VOLUME_SURGE_RATIOS:
        run_strategy(f'出来高急増{r:g}倍・株価横ばい（買いで検証）', 'LONG', volume_quiet_events[r])
        run_strategy(f'出来高急増{r:g}倍・株価横ばい（売りで検証）', 'SHORT', volume_quiet_events[r])

    os.makedirs(os.path.dirname(args.out) or '.', exist_ok=True)
    with open(args.out + '.json', 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    with open(args.out + '.md', 'w', encoding='utf-8') as f:
        f.write(render_markdown(report))
    print(f'[strategy] 完了: {args.out}.json / .md', flush=True)


def _pct(v, digits=2):
    return '—' if v is None else f'{v:+.{digits}f}%'


def render_markdown(rep):
    meta = rep.get('meta', {})
    code = meta.get('code') or {}
    lines = ['# スクリーニング・酒田五法 売買シミュレーション（先読みなし）', '',
             f"- 期間: {rep['period'][0]}〜{rep['period'][1]}（{rep['tickers']}銘柄、うち期間中の上場廃止 {rep.get('delisted_tickers', 0)}）",
             f"- 前半（ATR倍率などを選ぶ期間）: 〜{rep['split_date']}の前日 / 後半（選んだ値を確かめる期間）: {rep['split_date']}〜",
             f"- エントリー: シグナル翌営業日の始値 / 決済: 終値がストップに抵触した翌営業日の始値 / 往復コスト {rep['cost_pct_round_trip']}%",
             '- ファンダメンタルズは開示日がシグナル日以前の本決算のみ。PER等は実際の株価と開示後の分割だけで換算した1株あたり指標で計算。',
             '- 酒田五法の精度は前半期間だけで計測。保有中に上場廃止になった場合は最終取引日の終値で決済（「廃止決済」列）。',
             f"- 取引できない銘柄（売買代金不足・仕手株化の兆候）の日はシグナルを出さない（除外した銘柄×日: {rep.get('excluded_stock_days', 0):,}）。株価の上限は考慮しない。",
             '- 期間末に未決済のものは最終日の終値で評価（「未決済」列）。',
             '- 市場平均との差：同じ保有期間（シグナル日の終値→決済日の終値）の、取引可能な全銘柄の等金額平均との差。'
             'SHORTは市場平均の下落分を利益として比較。ATR倍率はこの差で選ぶ（長く持つだけで上昇相場に乗る効果を除くため）。',
             f"- 再現性: データ指紋 {meta.get('data_fingerprint_sha256', '')[:16]} / コード {str(code.get('git_commit'))[:8]}"
             f"{'（未コミットの変更あり・ファイルハッシュはJSON参照）' if code.get('git_dirty') else ''}", '',
             '## 前半期間で測った酒田五法の20日方向一致率（スコアの重みに使用）', '',
             '| パターン | 一致率 | 前半の検出数 |', '|---|---:|---:|']
    for key, cnt in sorted(rep['sakata_detections_first_half'].items(), key=lambda x: (-x[1], x[0])):
        acc = rep['sakata_accuracy_first_half'].get(key)
        name, direction = key.split('|')
        lines.append(f"| {name}（{'買い' if direction == 'bullish' else '売り'}） | {'' if acc is None else f'{acc}%'} | {cnt:,} |")
    lines.append('')
    for label, s in rep['strategies'].items():
        pick = s['picked_atr_by_first_half']
        lines += [f'## {label}', '',
                  f"前半期間で「市場平均を差し引いた期待値」が最大だったATR倍率: **{pick if pick else '選定なし（件数不足）'}**", '',
                  '| ATR倍率 | 期間 | 件数 | 勝率 | 平均利益 | 平均損失 | ペイオフ | 期待値/回 | 市場平均との差/回 | 差の勝率 | PF | 平均保有日 | 差/日 | 未決済 | 廃止決済 |',
                  '|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
        for m, periods in s['atr_grid'].items():
            for pname in ('前半（選ぶ期間）', '後半（確かめる期間）'):
                x = periods[pname]
                if not x.get('n'):
                    continue
                mark = ' ★' if m == pick else ''
                lines.append(
                    f"| {m}{mark} | {pname[:2]} | {x['n']:,} | {x['win_rate_pct']}% | {x['avg_win_pct']:+.2f}% | "
                    f"{x['avg_loss_pct']:+.2f}% | {x['payoff_ratio']} | {x['expectancy_pct']:+.2f}% | "
                    f"{_pct(x.get('excess_expectancy_pct'))} | {x.get('excess_win_rate_pct')}% | {x['profit_factor']} | "
                    f"{x['avg_hold_days']} | {_pct(x.get('excess_per_day_pct'), 3)} | {x['open_at_end']} | {x['delisted_exit']} |")
        lines.append('')
    return '\n'.join(lines)


if __name__ == '__main__':
    sys.exit(main())
