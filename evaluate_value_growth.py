#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
evaluate_value_growth.py — 割安（VALUE）／成長（GROWTH）の2本立てスコア（value_growth_scoring.py）を、
「保有期間の上限」を含む手仕舞いルールと総当たりで組み合わせ、勝率・ペイオフ・期待値・回転効率の
バランスが良いところを先読みなしで探す（2026-10-08追加。分析専用、本番コードには影響しない）。

【回転効率】
長く持つほど上昇相場に乗るだけで成績が良く見えるため、「1営業日あたりの市場平均との差」
（＝市場との差 ÷ 平均保有日数）を回転効率として並べる。同じ資金を何回転させられるかの目安。

【先読みを防ぐルール】（search_selection_rules.py と同じ）
- エントリー判定はパネル（build_panel.py、5営業日ごと）の t日の引けまでの特徴量だけ。順位は同じ日の取引可能銘柄内。
- エントリーは t+1日の始値。トレーリングは終値で判定し翌営業日の始値で決済。保有日数の上限はその日の終値で決済。
- 利確・損切り（ブラケット）は日中の高値・安値で約定、同じ日に両方触れたら損切り優先。往復コスト0.1%。
- 同じ銘柄・同じ組み合わせで保有中なら新規に建てない。
- 決算データは開示日がその日以前のものだけ。PER・PBRは「その日の実際の株価」と「その時点の株数に換算した
  1株あたり値」で計算（build_panel.py / fundamental_features.py）。上場廃止銘柄も含む。
- 【ルールの選び方（2026-10-08、kurosukeさんの要望：当時の状況だけで判断）】配点・手仕舞いは前半の成績だけで選び、
  後半は選んだものをそのまま当てはめて確かめる（後半の結果を見て選び直さない）。
- 残る限界：業種分類は現在のもの（_universe.json）を使っている。業種変更はまれなので影響は小さい。

【出力】data/backtest_out/value_growth_eval.json / .md
"""

import json
import os
import sys
from collections import defaultdict
from multiprocessing import Pool

import numpy as np
import pandas as pd

import repro
from evaluate_crashes import build_index
from evaluate_sakata import load_adjusted_bars, load_eval_universe
from fast_features import precompute_technicals
from value_growth_scoring import VALUE_WEIGHTS, VALUE_WEIGHTS_OLD, add_scores, rescore

OUT = 'data/backtest_out'
# 延長版キャッシュ（extend_bars_yfinance.py → build_panel.py --suffix _ext）で作ったパネルを使う場合は PANEL_SUFFIX=_ext
SUFFIX = os.getenv('PANEL_SUFFIX', '')
COST_PCT = 0.1
FALLBACK_ATR_PCT = 0.03
START_DATE = '2024-10-10'
SPLIT_DATE = '2025-08-08'   # evaluate_strategy.py / search_selection_rules.py と同じ境目
TOP_N = 5

EXIT_RULES = (
    [{'name': f'{T}日保有', 'kind': 'time', 'T': T} for T in (5, 10, 20, 40, 60)]
    + [{'name': f'トレーリングATR×{m}・最長{T}日', 'kind': 'trail', 'm': m, 'T': T}
       for m in (2.0, 3.0, 4.0) for T in (20, 40, 60)]
    + [{'name': f'利確ATR×{tp}・損切りATR×{sl}・最長{T}日', 'kind': 'bracket', 'tp': tp, 'sl': sl, 'T': T}
       for tp, sl in ((2, 1), (3, 1.5), (4, 2)) for T in (10, 20)]
)


VALUE_VARIANTS = {
    '旧配点：PER×PBR35・PBR20・PER15・現金15・配当15': VALUE_WEIGHTS_OLD,
    '分散：PER×PBR25・業種内割安20・現金20・配当15・自己資本10・業種内勢い10': VALUE_WEIGHTS,
    '業種重視：PER×PBR20・業種内割安30・現金20・配当15・業種内勢い15':
        {'per_pbr': 20, 'sec_per_pbr': 30, 'cash': 20, 'div': 15, 'sec_mom': 15},
}


def score_panel(p):
    """パネル（取引可能銘柄・START_DATE以降）に、日付ごとに value_growth_scoring のスコアを付ける。
    順位・業種内比較はすべて「同じ日の取引可能銘柄」の中だけで計算する（その日に分かる情報だけ）。"""
    p = p.rename(columns={'per_actual': 'per'})
    p['div_yield'] = p['div_yield_forecast'].fillna(p['div_yield_actual'])
    return pd.concat([add_scores(g) for _, g in p.groupby('date')], ignore_index=True)


def load_panel():
    p = pd.read_pickle(os.path.join(OUT, f'panel{SUFFIX}.pkl'))
    return p[(p['tradeable'] == True) & (p['date'] >= START_DATE)].copy()  # noqa: E712


def entry_sets():
    p = load_panel()
    scored = score_panel(p)

    pick = {}

    def top(mask, key, name, ascending=False, n=TOP_N):
        sub = scored[mask & scored[key].notna()].sort_values(['date', key, 'ticker'], ascending=[True, ascending, True])
        pick[name] = sub.groupby('date').head(n)[['ticker', 'date']]

    s = scored
    ma50 = s['above_ma50'] == True  # noqa: E712
    # 比較用：現行の一次条件（配当3.5〜5.8%・PER×PBR≦22.5）でPER×PBRの低い順
    top((s['div_yield'] >= 3.5) & (s['div_yield'] <= 5.8) & (s['per_pbr_calc'] <= 22.5), 'per_pbr_calc',
        '現行条件（配当3.5〜5.8%・PER×PBR≦22.5）PER×PBR順', ascending=True)
    # 割安の配点案（どれも金融業を除き、50日線より上）。前半の成績だけで1つ選び、後半はそのまま当てはめる
    for label, w in VALUE_VARIANTS.items():
        s[f'_v_{label}'] = rescore(s, 'value', w)
        top(s['value_gate'] & ma50, f'_v_{label}', f'VALUE[{label}]＋50日線より上')
    top(s['growth_gate'], 'growth_score', 'GROWTH スコア上位')
    top(s['financial_gate'], 'financial_score', 'FINANCIAL 金融スコア上位3', n=3)
    top(s['financial_gate'] & ma50, 'financial_score', 'FINANCIAL 金融スコア上位3＋50日線より上', n=3)

    sets = defaultdict(lambda: defaultdict(list))
    for name, df in pick.items():
        for t, d in zip(df['ticker'], df['date']):
            sets[name][t].append(d)
    return sets


def run_exit(O, H, L, C, A, i, rule, n):
    e = i + 1
    entry = O[e]
    a = A[i] if A[i] == A[i] and A[i] > 0 else entry * FALLBACK_ATR_PCT
    last = min(n - 1, e + rule['T'] - 1)
    kind = rule['kind']
    if kind == 'time':
        return last, C[last]
    if kind == 'trail':
        m = rule['m']
        stop = entry - a * m
        for j in range(e, last + 1):
            aj = A[j] if A[j] == A[j] and A[j] > 0 else C[j] * FALLBACK_ATR_PCT
            stop = max(stop, C[j] - aj * m)
            if C[j] <= stop and j < last:
                return (j + 1, O[j + 1]) if j + 1 < n else (j, C[j])
        return last, C[last]
    tp, sl = entry + a * rule['tp'], entry - a * rule['sl']
    for j in range(e, last + 1):
        if j > e and O[j] <= sl:
            return j, O[j]
        if j > e and O[j] >= tp:
            return j, O[j]
        if L[j] <= sl:
            return j, sl
        if H[j] >= tp:
            return j, tp
    return last, C[last]


def simulate_ticker(job):
    (ticker, delisted), set_dates = job
    df = load_adjusted_bars(ticker)
    if df is None or len(df) < 30:
        return []
    dates = [d.strftime('%Y-%m-%d') for d in df.index]
    pos = {d: k for k, d in enumerate(dates)}
    O, H, L, C = (df[c].to_numpy(float) for c in ('Open', 'High', 'Low', 'Close'))
    A = precompute_technicals(df)['atr']
    n = len(C)
    out = []
    for set_name, sig_dates in set_dates.items():
        idxs = sorted({pos[d] for d in sig_dates if d in pos})
        for r_k, rule in enumerate(EXIT_RULES):
            busy = -1
            for i in idxs:
                if i < busy or i + 1 >= n or not O[i + 1] > 0:
                    continue
                x, px = run_exit(O, H, L, C, A, i, rule, n)
                ret = (px / O[i + 1] - 1) * 100 - COST_PCT
                hold = max(1, x - i)
                incomplete = (x == n - 1) and not delisted and (x - i) < rule['T']
                out.append((set_name, r_k, dates[i], dates[x], ret, hold, incomplete, ticker))
                busy = x
    return out


def stats(trs, market):
    trs = [t for t in trs if not t[6]]  # 期間末で保有期間を満たせなかったものは除外
    n = len(trs)
    if not n:
        return {'n': 0}
    rets = np.array([t[4] for t in trs])
    wins, losses = rets[rets > 0], rets[rets <= 0]
    aw = float(wins.mean()) if len(wins) else 0.0
    al = float(losses.mean()) if len(losses) else 0.0
    ex = [t[4] - (market[t[3]] / market[t[2]] - 1) * 100 for t in trs if market.get(t[2]) and market.get(t[3])]
    hold = float(np.mean([t[5] for t in trs]))
    exm = float(np.mean(ex)) if ex else None
    # t値：シグナル日ごとに平均してから（同じ日の銘柄は同時に動くため）
    by_d = defaultdict(list)
    for t, x in zip(trs, ex):
        by_d[t[2]].append(x)
    dm = np.array([np.mean(v) for v in by_d.values()])
    tval = float(dm.mean() / (dm.std(ddof=1) / np.sqrt(len(dm)))) if len(dm) > 2 and dm.std() > 0 else None
    pf = float(wins.sum() / -losses.sum()) if len(losses) and losses.sum() < 0 else None
    return {'n': n, 'win_rate_pct': round(float((rets > 0).mean()) * 100, 1), 'avg_win_pct': round(aw, 2),
            'avg_loss_pct': round(al, 2), 'payoff_ratio': round(aw / abs(al), 2) if al else None,
            'profit_factor': round(pf, 2) if pf else None,
            'expectancy_pct': round(float(rets.mean()), 3), 'excess_pct': round(exm, 3) if exm is not None else None,
            'excess_t': round(tval, 2) if tval is not None else None,
            'avg_hold_days': round(hold, 1),
            'expectancy_per_day_pct': round(float(rets.mean()) / hold, 4),
            'excess_per_day_pct': round(exm / hold, 4) if exm is not None else None}


def main():
    workers = max(1, (os.cpu_count() or 2) - 1)
    universe = sorted(load_eval_universe())
    print('[vg] エントリー候補を作成中', flush=True)
    sets = entry_sets()
    by_ticker = defaultdict(dict)
    for name, tick in sets.items():
        for t, ds in tick.items():
            by_ticker[t][name] = ds
    delisted = dict(universe)
    jobs = [((t, delisted.get(t, False)), by_ticker[t]) for t in sorted(by_ticker)]
    print(f'[vg] シミュレーション：エントリー{len(sets)}種×手仕舞い{len(EXIT_RULES)}種、{len(jobs)}銘柄', flush=True)
    trades = defaultdict(list)
    with Pool(workers) as pool:
        for res in pool.imap(simulate_ticker, jobs, chunksize=4):
            for t in res:
                trades[(t[0], t[1])].append(t)

    idx, *_ = build_index(pd.read_pickle(os.path.join(OUT, f'daily{SUFFIX}.pkl')))
    market = idx['ew_level'].to_dict()
    results = []
    for (name, r_k), trs in sorted(trades.items()):
        results.append({'entry': name, 'exit': EXIT_RULES[r_k]['name'], 'exit_rule': EXIT_RULES[r_k],
                        'first': stats([t for t in trs if t[2] < SPLIT_DATE], market),
                        'second': stats([t for t in trs if t[2] >= SPLIT_DATE], market),
                        'all': stats(trs, market)})
    rep = {'meta': repro.run_metadata([t for t, _ in universe],
                                      {'exit_rules': EXIT_RULES, 'start': START_DATE, 'split': SPLIT_DATE,
                                       'top_n': TOP_N, 'cost_pct': COST_PCT},
                                      ['evaluate_value_growth.py', 'value_growth_scoring.py', 'build_panel.py']),
           'combinations_tested': len(results),
           'entry_sets': {k: sum(len(v) for v in tk.values()) for k, tk in sets.items()},
           'results': results}
    with open(os.path.join(OUT, 'value_growth_eval.json'), 'w', encoding='utf-8') as f:
        json.dump(rep, f, ensure_ascii=False, indent=2, default=str)
    with open(os.path.join(OUT, 'value_growth_eval.md'), 'w', encoding='utf-8') as f:
        f.write(render(rep))
    print('[vg] 完了', flush=True)


def _cells(x):
    if not x.get('n'):
        return '— | — | — | — | — | — | —'
    ex = '—' if x['excess_pct'] is None else f"{x['excess_pct']:+.2f}%"
    epd = '—' if x['excess_per_day_pct'] is None else f"{x['excess_per_day_pct']:+.3f}%"
    return (f"{x['n']:,} | {x['win_rate_pct']}% | {x['payoff_ratio']} | {x['expectancy_pct']:+.2f}% | {ex} | "
            f"{x['avg_hold_days']} | {epd}")


def render(rep):
    L = ['# 割安／成長スコア × 保有期間の上限つき手仕舞い（先読みなし）', '',
         f"- 前半＝〜{SPLIT_DATE}の前日、後半＝{SPLIT_DATE}〜。各日スコア上位{TOP_N}銘柄（パネルは5営業日ごと）。往復コスト{COST_PCT}%",
         '- 回転効率＝市場との差 ÷ 平均保有日数（1営業日あたり）',
         f"- 調べた組み合わせ：{rep['combinations_tested']}通り", '']
    for name in rep['entry_sets']:
        L += [f'## {name}（シグナル{rep["entry_sets"][name]:,}件）', '',
              '| 手仕舞い | 期間 | 件数 | 勝率 | ペイオフ | 期待値 | 市場との差 | 平均保有日 | 回転効率/日 |',
              '|---|---|---:|---:|---:|---:|---:|---:|---:|']
        for r in [r for r in rep['results'] if r['entry'] == name]:
            for part, lab in (('first', '前半'), ('second', '後半')):
                L.append(f"| {r['exit']} | {lab} | {_cells(r[part])} |")
        L.append('')
    return '\n'.join(L)


if __name__ == '__main__':
    sys.exit(main())
