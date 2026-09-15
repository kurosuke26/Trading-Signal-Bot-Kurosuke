#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
search_selection_rules.py — 「勝率60%台・ペイオフレシオ2前後」を狙える銘柄の選び方と手仕舞いの組み合わせを、
先読みなしで探す（2026-09-16追加）。

【考え方】
勝率とペイオフレシオは手仕舞いルール（利確幅・損切り幅・保有日数）でほぼ機械的にトレードオフになる。
勝率60%かつペイオフ2を両立するには、1回あたり「損切り幅の0.8倍」の優位性が必要で、エントリーの質が決め手になる。
そこで、これまでの検証で前半・後半とも効いた「エントリー候補」と、代表的な「手仕舞いルール」を総当たりし、
前半期間で目標（勝率60%以上・ペイオフ1.8以上・件数MIN_TRADES以上）を満たした組み合わせだけを、
後半期間でそのまま確かめる。組み合わせの数は出力に明記する（数が多いほど偶然の当たりが混ざるため）。

【先読みを防ぐルール（従来どおり）】
- エントリー判定はシグナル日の引けまでの情報（build_panel.py の特徴量・evaluate_strategy.py の本番判定・
  開示日基準の財務・分割換算・取引可能フィルター）だけ。エントリーはシグナル翌営業日の始値。
- 利確・損切り注文は事前に置いておく前提で、その日の高値・安値が触れたら約定。寄り付きで既に越えていたら始値で約定。
  同じ日に利確と損切りの両方に触れた場合は損切りを優先（保守的）。
- トレーリングは終値で判定し翌営業日の始値で決済（本番と同じ）。保有日数の上限は、その日の終値で決済。
- 本番LONGのスコアに使う酒田五法の精度は、strategy_eval_final.json の「前半期間で測った値」。
- 上場廃止は最終取引日の終値で決済。往復コスト0.1%。
- 市場との差：同じ保有期間の、取引可能な全銘柄の等金額平均との差（上昇相場の追い風を除いた実力）。

【出力】data/backtest_out/selection_rules_eval.json / .md
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
from evaluate_dividend_hikes import classify_events, dedup
from evaluate_sakata import load_adjusted_bars, load_eval_universe
from evaluate_strategy import classify, extract_ticker
from fast_features import precompute_technicals
from universe_filters import filter_params

OUT = 'data/backtest_out'
COST_PCT = 0.1
FALLBACK_ATR_PCT = 0.03
START_DATE = '2024-10-10'   # evaluate_strategy.py と同じ開始日（指標の助走期間の後）
SPLIT_DATE = '2025-08-08'   # evaluate_strategy.py と同じ前半／後半の境目
MIN_TRADES = 80
TARGET_WIN = 60.0
TARGET_PR_MIN = 1.8

EXIT_RULES = (
    [{'name': f'トレーリング ATR×{m}', 'kind': 'trail', 'm': m} for m in (2.0, 3.0, 4.0)]
    + [{'name': f'利確ATR×{tp}・損切りATR×{sl}・最長{T}日', 'kind': 'bracket', 'tp': tp, 'sl': sl, 'T': T}
       for tp, sl in ((2, 1), (3, 1.5), (4, 2), (6, 3)) for T in (20, 60)]
    + [{'name': '損切りATR×2→含み益ATR×2で建値ストップ→ATR×3トレーリング・最長60日', 'kind': 'hybrid',
        'sl': 2.0, 'be': 2.0, 'm': 3.0, 'T': 60}]
)


# ---------------------------------------------------------------------------
# エントリー候補
# ---------------------------------------------------------------------------

def _prod_long_rows(item):
    """evaluate_strategy.extract_ticker の特徴量から、本番LONGの (日付, スコア) だけを返す。"""
    res = extract_ticker(item)
    if not res:
        return None
    acc = _ACC
    out = []
    for row in res['rows']:
        if not row[6]:
            continue
        signal, score = classify(row, acc)
        if signal == 'LONG' and score is not None:
            out.append((res['dates'][row[0]], score))
    return item[0], out


_ACC = {}


def _init_acc(acc):
    global _ACC
    _ACC = acc


def build_entry_sets(universe, workers):
    sets = defaultdict(lambda: defaultdict(list))  # set_name -> ticker -> [signal_date]

    # A/B: 本番LONG（スコア上位5、毎日）
    with open(os.path.join(OUT, 'strategy_eval_final.json'), encoding='utf-8') as f:
        acc_raw = json.load(f)['sakata_accuracy_first_half']
    acc = {tuple(k.split('|')): v for k, v in acc_raw.items()}
    by_date = defaultdict(list)
    with Pool(workers, initializer=_init_acc, initargs=(acc,)) as pool:
        for res in pool.imap(_prod_long_rows, universe, chunksize=2):
            if not res:
                continue
            t, rows = res
            for d, s in rows:
                by_date[d].append((s, t))
    for thr in (70, 75):
        name = f'本番LONG（スコア{thr}点以上・上位5）'
        for d in sorted(by_date):
            for s, t in sorted([x for x in by_date[d] if x[0] >= thr], key=lambda x: (-x[0], x[1]))[:5]:
                sets[name][t].append(d)

    # C〜F: パネル（5営業日ごと）の割安系
    p = pd.read_pickle(os.path.join(OUT, 'panel.pkl'))
    p = p[p['tradeable'] & (p['date'] >= START_DATE)].copy()
    p['breadth50'] = p.groupby('date')['above_ma50'].transform(lambda s: s.astype(float).mean())
    p['rank_pbr'] = p.groupby('date')['pbr'].rank(pct=True)
    p['rank_per'] = p.groupby('date')['per_actual'].rank(pct=True)
    value_cash = (p['pbr'] <= 1.0) & (p['per_forecast'] > 0) & (p['per_forecast'] <= 12) & (p['cash_to_mktcap'] >= 0.3)
    low_pb_pe = (p['rank_pbr'] <= 0.2) & (p['rank_per'] <= 0.2)
    defs = {
        '割安×現金（PBR≦1・予想PER≦12・現金/時価総額≧30%）': value_cash,
        '低PBR×低PER（どちらも市場の下位20%）': low_pb_pe,
        '割安×現金＋押し目（25日線から−3%以下）': value_cash & (p['dist_ma25'] <= -0.03),
        '割安×現金＋地合い良好（50日線より上の銘柄が過半）': value_cash & (p['breadth50'] >= 0.5),
    }
    for name, mask in defs.items():
        sub = p[mask].sort_values(['date', 'pbr', 'ticker'])
        for d, g in sub.groupby('date'):
            for t in g['ticker'].head(10):  # 1日最大10銘柄（PBRの低い順）
                sets[name][t].append(d)

    daily = pd.read_pickle(os.path.join(OUT, 'daily.pkl'))
    idx, close, opn, trad, _ = build_index(daily)
    dates = list(idx.index)

    # G: 暴落後（指数が20日高値から−10%以下の日。暴落前20日高値から−20%以上下げた取引可能銘柄）
    dd = idx['ew_dd20'].to_numpy()
    last = -10 ** 9
    for k in range(25, len(dates)):
        if dd[k] <= -0.10 and k - last >= 20:
            last = k
            if dates[k] < START_DATE:
                continue
            hi20 = close.iloc[max(0, k - 20):k + 1].max()
            drop = close.iloc[k] / hi20 - 1
            ok = trad.iloc[k] & (drop <= -0.20)
            for t in ok[ok].index:
                sets['暴落後（指数−10%の日に、20日高値から−20%以上下げた銘柄）'][t].append(dates[k])

    # H: 増配修正の発表（発表日＝シグナル日、翌営業日の始値で買う）
    ev = classify_events(pd.read_pickle(os.path.join(OUT, 'dividend_events.pkl')))
    hk = dedup(ev[ev['kind'] == '増配修正'], dates)
    pos = {d: k for k, d in enumerate(dates)}
    for _, e in hk.iterrows():
        k = int(np.searchsorted(dates, e['disc_date']))
        if k >= len(dates) or dates[k] < START_DATE or e['ticker'] not in trad.columns:
            continue
        if bool(trad[e['ticker']].iat[max(0, k - 1)]):
            sets['増配修正の発表翌日'][e['ticker']].append(dates[k])
    return sets, idx['ew_level'].to_dict()


# ---------------------------------------------------------------------------
# 手仕舞いシミュレーション
# ---------------------------------------------------------------------------

def run_exit(O, H, L, C, A, i, rule, n):
    """i=シグナル日。戻り値 (exit_idx, exit_price)。"""
    e = i + 1
    entry = O[e]
    a = A[i] if A[i] == A[i] and A[i] > 0 else entry * FALLBACK_ATR_PCT
    kind = rule['kind']
    if kind == 'trail':
        m = rule['m']
        stop = entry - a * m
        for j in range(e, n):
            aj = A[j] if A[j] == A[j] and A[j] > 0 else C[j] * FALLBACK_ATR_PCT
            stop = max(stop, C[j] - aj * m)
            if C[j] <= stop:
                return (j + 1, O[j + 1]) if j + 1 < n else (j, C[j])
        return n - 1, C[n - 1]
    if kind == 'bracket':
        tp, sl = entry + a * rule['tp'], entry - a * rule['sl']
        last = min(n - 1, e + rule['T'] - 1)
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
    # hybrid
    stop = entry - a * rule['sl']
    armed = False
    last = min(n - 1, e + rule['T'] - 1)
    for j in range(e, last + 1):
        if j > e and O[j] <= stop:
            return j, O[j]
        if L[j] <= stop:
            return j, stop
        if not armed and H[j] >= entry + a * rule['be']:
            armed = True
        if armed:
            aj = A[j] if A[j] == A[j] and A[j] > 0 else C[j] * FALLBACK_ATR_PCT
            stop = max(stop, entry, C[j] - aj * rule['m'])
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
                open_end = (x == n - 1) and not delisted and rule['kind'] == 'trail'
                out.append((set_name, r_k, dates[i], dates[x], ret, x - i, open_end))
                busy = x
    return out


def stats(trs, market):
    n = len(trs)
    if not n:
        return {'n': 0}
    rets = np.array([t[4] for t in trs])
    wins, losses = rets[rets > 0], rets[rets <= 0]
    aw = wins.mean() if len(wins) else 0.0
    al = losses.mean() if len(losses) else 0.0
    ex = []
    for t in trs:
        a, b = market.get(t[2]), market.get(t[3])
        if a and b:
            ex.append(t[4] - (b / a - 1) * 100)
    hold = float(np.mean([t[5] for t in trs]))
    return {'n': n, 'win_rate_pct': round(float((rets > 0).mean()) * 100, 1), 'avg_win_pct': round(float(aw), 2),
            'avg_loss_pct': round(float(al), 2), 'payoff_ratio': round(float(aw / abs(al)), 2) if al else None,
            'expectancy_pct': round(float(rets.mean()), 3),
            'excess_pct': round(float(np.mean(ex)), 3) if ex else None,
            'excess_win_rate_pct': round(float(np.mean([x > 0 for x in ex])) * 100, 1) if ex else None,
            'avg_hold_days': round(hold, 1), 'open_at_end': int(sum(t[6] for t in trs))}


def main():
    workers = max(1, (os.cpu_count() or 2) - 1)
    universe = sorted(load_eval_universe())
    print(f'[select] エントリー候補を作成中（{len(universe)}銘柄）', flush=True)
    sets, market = build_entry_sets(universe, workers)
    by_ticker = defaultdict(dict)
    for set_name, tick in sets.items():
        for t, ds in tick.items():
            by_ticker[t][set_name] = ds
    delisted = dict(universe)
    jobs = [((t, delisted.get(t, False)), by_ticker[t]) for t in sorted(by_ticker)]
    print(f'[select] シミュレーション：エントリー候補{len(sets)}種×手仕舞い{len(EXIT_RULES)}種、{len(jobs)}銘柄', flush=True)

    trades = defaultdict(list)
    with Pool(workers) as pool:
        for res in pool.imap(simulate_ticker, jobs, chunksize=4):
            for t in res:
                trades[(t[0], t[1])].append(t)

    results = []
    for (set_name, r_k), trs in sorted(trades.items()):
        first = [t for t in trs if t[2] < SPLIT_DATE]
        second = [t for t in trs if t[2] >= SPLIT_DATE]
        f, s = stats(first, market), stats(second, market)
        meets_first = (f.get('n', 0) >= MIN_TRADES and f['win_rate_pct'] >= TARGET_WIN
                       and (f['payoff_ratio'] or 0) >= TARGET_PR_MIN)
        meets_second = (s.get('n', 0) >= MIN_TRADES and s['win_rate_pct'] >= TARGET_WIN
                        and (s['payoff_ratio'] or 0) >= TARGET_PR_MIN)
        results.append({'entry': set_name, 'exit': EXIT_RULES[r_k]['name'], 'first': f, 'second': s, 'all': stats(trs, market),
                        'meets_target_first': meets_first, 'meets_target_second': meets_second})

    report = {'meta': repro.run_metadata([t for t, _ in universe],
                                         {'exit_rules': EXIT_RULES, 'start': START_DATE, 'split': SPLIT_DATE,
                                          'min_trades': MIN_TRADES, 'target_win': TARGET_WIN, 'target_pr_min': TARGET_PR_MIN,
                                          'cost_pct': COST_PCT, 'universe_filter': filter_params()},
                                         ['search_selection_rules.py', 'evaluate_strategy.py', 'build_panel.py',
                                          'universe_filters.py', 'sakata.py']),
              'combinations_tested': len(results), 'entry_sets': {k: sum(len(v) for v in tk.values()) for k, tk in sets.items()},
              'results': results}
    with open(os.path.join(OUT, 'selection_rules_eval.json'), 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    with open(os.path.join(OUT, 'selection_rules_eval.md'), 'w', encoding='utf-8') as f:
        f.write(render(report))
    print('[select] 完了', flush=True)


def _row(r, part):
    x = r[part]
    if not x.get('n'):
        return '— | — | — | — | —'
    ex = '—' if x['excess_pct'] is None else f"{x['excess_pct']:+.2f}%"
    return f"{x['n']:,} | {x['win_rate_pct']}% | {x['payoff_ratio']} | {x['expectancy_pct']:+.2f}% | {ex}"


def render(rep):
    L = ['# 勝率60%台・ペイオフ2前後を狙う選び方の探索（先読みなし）', '',
         f"- 前半＝〜{SPLIT_DATE}の前日（選ぶ期間）、後半＝{SPLIT_DATE}〜（確かめる期間）。目標：勝率{TARGET_WIN:.0f}%以上かつペイオフ{TARGET_PR_MIN}以上、件数{MIN_TRADES}以上",
         f"- 調べた組み合わせ：{rep['combinations_tested']}通り（エントリー候補×手仕舞いルール）",
         '- エントリー候補の件数（シグナル数）：' + ' / '.join(f'{k} {v:,}' for k, v in rep['entry_sets'].items()), '',
         '## 前半で目標を満たした組み合わせ → 後半の結果', '',
         '| エントリー | 手仕舞い | 前半 件数 | 前半 勝率 | 前半 ペイオフ | 前半 期待値 | 前半 市場との差 | 後半 件数 | 後半 勝率 | 後半 ペイオフ | 後半 期待値 | 後半 市場との差 | 後半も目標達成 |',
         '|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|']
    chosen = [r for r in rep['results'] if r['meets_target_first']]
    for r in sorted(chosen, key=lambda r: -r['first']['expectancy_pct']):
        L.append(f"| {r['entry']} | {r['exit']} | {_row(r, 'first')} | {_row(r, 'second')} | {'◎' if r['meets_target_second'] else '×'} |")
    if not chosen:
        L.append('| （前半で目標を満たした組み合わせなし） | | | | | | | | | | | | |')
    L += ['', '## 全組み合わせ（後半の勝率が高い順）', '',
          '| エントリー | 手仕舞い | 前半 件数 | 前半 勝率 | 前半 ペイオフ | 前半 期待値 | 前半 市場との差 | 後半 件数 | 後半 勝率 | 後半 ペイオフ | 後半 期待値 | 後半 市場との差 |',
          '|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    for r in sorted(rep['results'], key=lambda r: -(r['second'].get('win_rate_pct') or 0)):
        L.append(f"| {r['entry']} | {r['exit']} | {_row(r, 'first')} | {_row(r, 'second')} |")
    return '\n'.join(L)


if __name__ == '__main__':
    sys.exit(main())
