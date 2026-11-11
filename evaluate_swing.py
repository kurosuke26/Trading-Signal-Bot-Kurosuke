#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
evaluate_swing.py — スイング3型（swing_strategies.py）の検証（2026-11-11追加。先読みなし）。

- データ：J-Quants の日足（分割調整済み）と財務・業績予想（fins.json）。上場廃止銘柄も含む
- シグナル：swing_strategies.py と同じ関数（本番の毎日の判定と同じ式）
- 約定：シグナル翌営業日の始値。決済：swing_strategies.exit_index（保有期間の上限とストップの早いほう）
- 保有中に上場廃止 → 最終取引日の終値。期間末に未決済 → 最終日の終値で評価
- 型ごとに「手仕舞い3通り×保有期間3通り×地合いフィルターのオン/オフ＝18通り」を、walk-forward
  （evaluate_walkforward.py と同じ区間・同じ選び方）で毎回選び直した成績と、
  事前に決めた標準設定（10営業日・25日線割れ・地合いフィルターあり）を固定した成績を出す
- 成績は日次（validation.py）。p値・Holm/BH・Reality Check（54通り）・コスト0.1/0.3/0.5%

【実行方法】
  python evaluate_swing.py --max-tickers 200
  python evaluate_swing.py
出力: data/backtest_out/swing_eval.json / .md
"""

import argparse
import json
import os
from collections import defaultdict
from multiprocessing import Pool

import numpy as np
import pandas as pd

import repro
import swing_strategies as sw
import validation as v
from evaluate_sakata import load_adjusted_bars, load_eval_universe
from evaluate_walkforward import COSTS, MEAN_BLOCK, N_BOOT, make_folds, run_family_wf
from universe_filters import tradeable_flags

CACHE_DIR = os.getenv('JQUANTS_CACHE_DIR') or 'data/jquants_cache'
WARMUP_ROWS = 140   # 120日の上昇率・75日線の向きが計算できるまで
DEFAULT_CFG = (10, 'ma25', True)  # 事前に決めた標準設定（保有上限・ストップ・地合いフィルター）
SLOTS = 10  # 資金を10枠に分け、1ポジション＝1枠（validation.daily_series の slots）


# ---------------------------------------------------------------------------
# 上方修正の開示（J-Quants fins.json）
# ---------------------------------------------------------------------------

def _num(v):
    try:
        return None if v in (None, '') else float(v)
    except (TypeError, ValueError):
        return None


def revision_disclosures(ticker4):
    """同じ期の会社予想（営業利益→経常→純利益）が前回予想から +REVISION_MIN_PCT%以上 上がった開示 [(日付, 時刻)]。"""
    path = os.path.join(CACHE_DIR, ticker4, 'fins.json')
    if not os.path.exists(path):
        return []
    with open(path, encoding='utf-8') as f:
        rows = json.load(f)
    rows = sorted(rows, key=lambda r: (r.get('DiscDate') or '', r.get('DiscTime') or ''))
    last = {}  # (期末, 項目) -> 前回の予想
    out = []
    for r in rows:
        doc = r.get('DocType') or ''
        per = r.get('CurPerType') or ''
        if 'FinancialStatements' in doc and per == 'FY':
            fy, p = r.get('NxtFYEn') or '', 'NxF'
            vals = {'op': _num(r.get('NxFOP')) or _num(r.get('NxFNCOP')),
                    'odp': _num(r.get('NxFOdP')) or _num(r.get('NxFNCOdP')),
                    'np': _num(r.get('NxFNp')) or _num(r.get('NxFNCNP'))}
            is_revision_doc = False  # 翌期予想の初回公表は「修正」ではない
        elif 'FinancialStatements' in doc or doc == 'EarnForecastRevision':
            fy, p = r.get('CurFYEn') or '', 'F'
            vals = {'op': _num(r.get('FOP')) or _num(r.get('FNCOP')),
                    'odp': _num(r.get('FOdP')) or _num(r.get('FNCOdP')),
                    'np': _num(r.get('FNP')) or _num(r.get('FNCNP'))}
            is_revision_doc = True
        else:
            continue
        if not fy:
            continue
        # 判定に使う項目：営業利益 → 経常利益 → 純利益 の順で、今回の値があるもの
        key = next((k for k in ('op', 'odp', 'np') if vals.get(k) is not None), None)
        old = last.get((fy, key)) if key else None
        hit = bool(is_revision_doc and old is not None and old > 0
                   and (vals[key] / old - 1) * 100 >= sw.REVISION_MIN_PCT)
        for k, x in vals.items():
            if x is not None:
                last[(fy, k)] = x
        if hit:
            out.append((r.get('DiscDate'), (r.get('DiscTime') or '')[:5]))
    return out


# ---------------------------------------------------------------------------
# 1銘柄分の特徴量・シグナル
# ---------------------------------------------------------------------------

def extract_ticker(item):
    ticker4, is_delisted = item
    loaded = load_adjusted_bars(ticker4, return_raw=True)
    if loaded is None:
        return None
    df, raw = loaded
    if len(df) < WARMUP_ROWS + 5:
        return None
    f = sw.compute_features(df)
    trad, _ = tradeable_flags(raw, df, f['atr'].to_numpy())
    dates = [d.strftime('%Y-%m-%d') for d in df.index]
    react = sw.reaction_dates(dates, revision_disclosures(ticker4))
    reaction = pd.Series([d in react for d in dates], index=df.index)
    masks = {sw.PULLBACK: sw.pullback_mask(f), sw.BREAKOUT: sw.breakout_mask(f),
             sw.REVISION: sw.revision_mask(f, reaction)}
    warm = np.arange(len(df)) >= WARMUP_ROWS - 1
    out = {'t': ticker4, 'dates': dates, 'O': df['Open'].to_numpy(float), 'C': df['Close'].to_numpy(float),
           'ma25': f['ma25'].to_numpy(float), 'atr': f['atr'].to_numpy(float), 'trad': np.asarray(trad, bool),
           'delisted': is_delisted, 'sig': {}, 'n_revisions': len(react)}
    for k, m in masks.items():
        idx = np.flatnonzero(m.to_numpy(bool) & warm & out['trad'])
        rk = sw.rank_key(k, f).to_numpy(float)
        out['sig'][k] = [(int(i), float(rk[i]) if rk[i] == rk[i] else -1e9) for i in idx]
    return out


# ---------------------------------------------------------------------------
# 売買シミュレーション
# ---------------------------------------------------------------------------

def simulate(td, sig_idx, hold, stop):
    """sig_idx: シグナル日のindex（昇順）。戻り値：日次系列用の legs と、1トレードの成績。"""
    O, C, n = td['O'], td['C'], len(td['C'])
    legs, busy = [], -1
    for i in sig_idx:
        e = i + 1
        if i < busy or e >= n or not O[e] > 0:
            continue
        x, at_close = sw.exit_index(O, C, td['ma25'], td['atr'], e, hold, stop)
        open_end = False
        if x is None:
            x, at_close, open_end = n - 1, True, not td['delisted']
        px = C[x] if at_close else O[x]
        legs.append({'g': td['g'], 'O': O, 'C': C, 'e': e, 'x': x, 'x_at_close': at_close, 'side': 'LONG',
                     'ret': (px / O[e] - 1) * 100, 'hold': x - e + 1, 'open_end': open_end, 'd': td['dates'][i]})
        busy = x
    return legs


def trade_stats(legs, cost, mkt_level, gpos):
    if not legs:
        return {'n': 0}
    rets = np.array([l['ret'] - cost for l in legs])
    wins, losses = rets[rets > 0], rets[rets <= 0]
    ex = []
    for l in legs:
        a, b = mkt_level[l['g'][l['e']] - 1], mkt_level[l['g'][l['x']]]
        if a > 0:
            ex.append(l['ret'] - cost - (b / a - 1) * 100)
    return {'n': len(legs), 'win_rate_pct': round(len(wins) / len(rets) * 100, 1),
            'avg_win_pct': round(float(wins.mean()), 2) if len(wins) else 0.0,
            'avg_loss_pct': round(float(losses.mean()), 2) if len(losses) else 0.0,
            'payoff_ratio': round(float(wins.mean() / -losses.mean()), 2) if len(wins) and len(losses) and losses.mean() else None,
            'expectancy_pct': round(float(rets.mean()), 3),
            'excess_expectancy_pct': round(float(np.mean(ex)), 3) if ex else None,
            'avg_hold_days': round(float(np.mean([l['hold'] for l in legs])), 1),
            'open_at_end': int(sum(l['open_end'] for l in legs))}


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--max-tickers', type=int, default=0)
    ap.add_argument('--workers', type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument('--out', default='data/backtest_out/swing_eval')
    args = ap.parse_args()

    universe = sorted(load_eval_universe(args.max_tickers))
    print(f'[swing] 特徴量の計算: {len(universe)}銘柄', flush=True)
    with Pool(args.workers) as pool:
        data = [r for r in pool.imap(extract_ticker, universe, chunksize=4) if r]
    data.sort(key=lambda td: td['t'])

    # 全体の日付・市場平均（前日に取引可能だった銘柄の等金額平均）・地合い（指数が75日線より上）
    dates = sorted({d for td in data for d in td['dates']})
    pos = {d: k for k, d in enumerate(dates)}
    n_dates = len(dates)
    s_r, cnt = np.zeros(n_dates), np.zeros(n_dates)
    for td in data:
        td['g'] = np.array([pos[d] for d in td['dates']], dtype=np.int64)
        r = td['C'][1:] / td['C'][:-1] - 1
        ok = td['trad'][:-1] & np.isfinite(r)
        np.add.at(s_r, td['g'][1:][ok], r[ok])
        np.add.at(cnt, td['g'][1:][ok], 1.0)
    mkt = np.divide(s_r, cnt, out=np.zeros(n_dates), where=cnt > 0)
    level = np.cumprod(1 + mkt)
    regime_ok = level > pd.Series(level).rolling(75).mean().to_numpy()
    print(f'[swing] 期間 {dates[0]}〜{dates[-1]} / 地合い良好の日 {int(np.nansum(regime_ok))}/{n_dates}', flush=True)

    # 型 × 地合いフィルター → 銘柄ごとのシグナル日（1日あたり上位 TOP_N）
    cfg_series, cfg_legs = {}, {}
    sig_counts = {}
    for kind in sw.TYPES:
        for use_regime in sw.REGIME_GRID:
            by_date = defaultdict(list)
            for ti, td in enumerate(data):
                for i, rk in td['sig'][kind]:
                    if use_regime and not regime_ok[td['g'][i]]:
                        continue
                    by_date[td['dates'][i]].append((rk, td['t'], i, ti))
            cands = defaultdict(list)
            for d in sorted(by_date):
                for _, _, i, ti in sorted(by_date[d], key=lambda x: (-x[0], x[1]))[:sw.TOP_N]:
                    cands[ti].append(i)
            sig_counts[f'{kind}|{use_regime}'] = sum(len(x) for x in cands.values())
            for hold in sw.HOLD_GRID:
                for stop in sw.STOP_GRID:
                    legs = []
                    for ti in sorted(cands):
                        legs += simulate(data[ti], sorted(cands[ti]), hold, stop)
                    key = (kind, hold, stop, use_regime)
                    cfg_legs[key] = legs
                    cfg_series[key] = {c: v.daily_series(legs, n_dates, mkt, cost_pct=c, slots=SLOTS) for c in COSTS}
        print(f'[swing] {sw.TYPE_LABEL[kind]}: シグナル {sig_counts[f"{kind}|False"]}件'
              f'（地合いフィルターあり {sig_counts[f"{kind}|True"]}件）', flush=True)

    first_k = WARMUP_ROWS - 1
    folds = make_folds(dates, first_k)
    oos_lo = folds[0][0]
    oos_idx = v.stationary_bootstrap_indices(n_dates - oos_lo, N_BOOT, MEAN_BLOCK)
    print('[swing] 区間: ' + ', '.join(f'{dates[s]}〜{dates[e - 1]}' for s, e in folds), flush=True)

    families = {}
    for kind in sw.TYPES:
        sub = {k: s for k, s in cfg_series.items() if k[0] == kind}
        families[f'{sw.TYPE_LABEL[kind]}（手仕舞い・保有期間・地合いを毎回選ぶ）'] = (lambda d: (lambda s: d))(sub)
        dk = (kind,) + DEFAULT_CFG
        families[f'{sw.TYPE_LABEL[kind]}（標準設定で固定）'] = (lambda d: (lambda s: d))({dk: cfg_series[dk]})

    report = {'period': [dates[first_k], dates[-1]], 'oos_period': [dates[oos_lo], dates[-1]],
              'folds': [[dates[s], dates[e - 1]] for s, e in folds], 'tickers': len(data),
              'signal_counts': sig_counts, 'families': {}, 'grid_trades': {}}
    for cost in COSTS:
        rows = []
        for name, fn in families.items():
            oos, picks = run_family_wf(name, folds, first_k, n_dates, fn, cost)
            ex = oos['excess'][oos_lo:]
            rows.append({'name': name, 'excess': v.summarize(ex, active=oos['count'][oos_lo:] > 0),
                         'raw': v.summarize(oos['raw'][oos_lo:]), 'test': v.bootstrap_test(ex, oos_idx),
                         'folds': [{'period': [dates[s], dates[e - 1]], 'picked': _cfg_label(pk['picked']),
                                    'excess_total_pct': v.summarize(oos['excess'][s:e])['total_pct']}
                                   for (s, e), pk in zip(folds, picks)]})
        p = [r['test']['p_value'] for r in rows]
        for r, h, q in zip(rows, v.holm(p), v.benjamini_hochberg(p)):
            r['test']['p_holm'], r['test']['q_bh'] = round(float(h), 4), round(float(q), 4)
        report['families'][str(cost)] = rows

    # 1トレードあたりの成績（全期間・後半＝walk-forward の検証期間）。コスト0.3%
    for key, legs in sorted(cfg_legs.items(), key=lambda kv: str(kv[0])):
        oos_legs = [l for l in legs if l['d'] >= dates[oos_lo]]
        report['grid_trades'][_cfg_label(key, with_kind=True)] = {
            '全期間': trade_stats(legs, 0.3, level, pos), '検証期間': trade_stats(oos_legs, 0.3, level, pos)}

    names = sorted(cfg_series, key=str)
    X = np.array([cfg_series[k][0.3]['excess'][first_k:] for k in names])
    t_obs, adj, p_best = v.max_t_adjusted(X, v.stationary_bootstrap_indices(X.shape[1], N_BOOT, MEAN_BLOCK))
    order = np.argsort(-t_obs)
    report['reality_check'] = {'n_configs': len(names), 'p_best': round(p_best, 4), 'cost_pct': 0.3,
                               'top': [{'name': _cfg_label(names[i], with_kind=True), 't': round(float(t_obs[i]), 2),
                                        'p_adjusted': round(float(adj[i]), 4), 'summary': v.summarize(X[i])}
                                       for i in order[:15]]}
    report['meta'] = repro.run_metadata([t for t, _ in universe], {
        'top_n': sw.TOP_N, 'hold_grid': sw.HOLD_GRID, 'stop_grid': sw.STOP_GRID, 'regime_grid': sw.REGIME_GRID,
        'default_cfg': DEFAULT_CFG, 'revision_min_pct': sw.REVISION_MIN_PCT, 'warmup_rows': WARMUP_ROWS,
        'costs': COSTS, 'slots': SLOTS, 'n_boot': N_BOOT, 'mean_block': MEAN_BLOCK},
        ['evaluate_swing.py', 'swing_strategies.py', 'validation.py', 'evaluate_walkforward.py', 'universe_filters.py'])
    os.makedirs(os.path.dirname(args.out) or '.', exist_ok=True)
    with open(args.out + '.json', 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    with open(args.out + '.md', 'w', encoding='utf-8') as f:
        f.write(render_markdown(report))
    print(f'[swing] 完了: {args.out}.json / .md', flush=True)


def _cfg_label(key, with_kind=False):
    if key is None:
        return None
    kind, hold, stop, reg = key
    s = f'{hold}日・{sw.STOP_LABEL[stop]}・地合い{"あり" if reg else "なし"}'
    return f'{sw.TYPE_LABEL[kind]} {s}' if with_kind else s


def _p(x, d=2):
    return '—' if x is None else f'{x:+.{d}f}%'


def render_markdown(rep):
    meta = rep.get('meta', {})
    code = meta.get('code') or {}
    L = ['# スイング3型の検証（walk-forward・先読みなし）', '',
         f"- 期間: {rep['period'][0]}〜{rep['period'][1]}（{rep['tickers']}銘柄）／walk-forward の検証期間: "
         f"{rep['oos_period'][0]}〜（区間: {' / '.join(f'{a}〜{b}' for a, b in rep['folds'])}）",
         '- 約定は翌営業日の始値。手仕舞いは保有期間の上限（その日の引け）とストップ（ATR×2／ATR×3 の切り上げ、'
         'または25日線割れ→翌営業日の始値）の早いほう。1日あたり各型上位5件。',
         f"- 標準設定（事前に決めたもの）: {DEFAULT_CFG[0]}営業日・{sw.STOP_LABEL[DEFAULT_CFG[1]]}・地合いフィルターあり"
         '（市場平均が75日線より上の日だけ買う）',
         f'- 成績は日次。資金を{SLOTS}枠に分け1ポジション＝1枠（空き枠は現金、{SLOTS}件を超えたら等分）。'
         '市場との差＝各ポジションの（リターン − 取引可能な全銘柄の等金額平均）。',
         f"- 再現性: データ指紋 {meta.get('data_fingerprint_sha256', '')[:16]} / コード {str(code.get('git_commit'))[:8]}"
         f"{'（未コミットの変更あり）' if code.get('git_dirty') else ''}", '',
         '## シグナル数（全期間・1日上位5件に絞った後）', '', '| 型 | 地合いフィルターなし | あり |', '|---|---:|---:|']
    for k in sw.TYPES:
        L.append(f"| {sw.TYPE_LABEL[k]} | {rep['signal_counts'][f'{k}|False']:,} | {rep['signal_counts'][f'{k}|True']:,} |")
    for cost, rows in rep['families'].items():
        L += ['', f'## walk-forward の成績（往復コスト {cost}%）', '',
              '| 戦略 | 市場との差 年率 | IR | 最大DD | p値 | Holm | 単純リターン 年率 | 保有のある日 | 区間ごと（選んだ設定） |',
              '|---|---:|---:|---:|---:|---:|---:|---:|---|']
        for r in rows:
            ex, t = r['excess'], r['test']
            fl = ' / '.join(f"{_p(f['excess_total_pct'], 1)}（{f['picked'] or '見送り'}）" for f in r['folds'])
            L.append(f"| {r['name']} | {_p(ex['annual_pct'])} | {ex['ir_annual'] if ex['ir_annual'] is not None else '—'} | "
                     f"{_p(ex['max_drawdown_pct'])} | {t['p_value']:.3f} | {t['p_holm']:.3f} | {_p(r['raw']['annual_pct'])} | "
                     f"{ex['active_days']}/{ex['days']} | {fl} |")
    L += ['', '## 1トレードあたりの成績（往復コスト0.3%。検証期間＝walk-forward の検証期間に入ったトレード）', '',
          '| 設定 | 期間 | 件数 | 勝率 | 平均利益 | 平均損失 | ペイオフ | 期待値 | 市場との差 | 平均保有日 |',
          '|---|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for name, per in rep['grid_trades'].items():
        for label, s in per.items():
            if not s.get('n'):
                continue
            L.append(f"| {name} | {label} | {s['n']:,} | {s['win_rate_pct']}% | {_p(s['avg_win_pct'])} | {_p(s['avg_loss_pct'])} | "
                     f"{s['payoff_ratio']} | {_p(s['expectancy_pct'])} | {_p(s['excess_expectancy_pct'])} | {s['avg_hold_days']} |")
    rc = rep['reality_check']
    L += ['', f"## Reality Check：{rc['n_configs']}通りの中の上位（全期間・コスト{rc['cost_pct']}%）", '',
          f"一番良く見える設定でも、偶然これ以上になる確率（多重比較の補正済み）は **{rc['p_best']:.3f}**。", '',
          '| 設定 | 市場との差 年率 | IR | t値 | 補正p値 |', '|---|---:|---:|---:|---:|']
    for r in rc['top']:
        L.append(f"| {r['name']} | {_p(r['summary']['annual_pct'])} | {r['summary']['ir_annual']} | {r['t']} | {r['p_adjusted']:.3f} |")
    return '\n'.join(L) + '\n'


if __name__ == '__main__':
    main()
