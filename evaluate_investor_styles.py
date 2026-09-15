#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
evaluate_investor_styles.py — investor_models.py の投資家タイプ別モデルを、特徴量パネル（build_panel.py）で
検証し、「勝ちが出やすい条件」を前半期間で探して後半期間で確かめる（2026-09-16追加）。

【測り方】
- 対象：各パネル日（5営業日ごと）に取引可能な銘柄（universe_filters：売買代金不足・仕手株化を除外）
- 成績：翌営業日の始値で買い、20営業日後・60営業日後の終値までの上昇率（上場廃止は最終取引日の終値）
- 超過：同じ日に取引可能だった全銘柄の平均上昇率との差（地合いを除いた実力）
- t値：日付ごとの超過の平均を、保有期間が重ならない間隔（20日なら4パネル日おき）で間引いて計算
  （同じ日に銘柄が集中する・保有期間が重なることによる見かけの有意差を避ける）
- 前半／後半：パネル日の中央で2分割。条件探索は前半で「効いていそうな条件」を選び、後半で確かめる

【出力】data/backtest_out/investor_styles_eval.json / .md
"""

import itertools
import json
import os
import sys

import numpy as np
import pandas as pd

import repro
from investor_models import MODELS, add_cross_sectional_ranks, evaluate_model
from universe_filters import filter_params

OUT_DIR = 'data/backtest_out'
HORIZONS = (20, 60)
PANEL_STEP = 5
MIN_NAMES_PER_DATE = 5
MIN_DATES = 8


def date_excess_series(df, mask, h):
    col = f'fwd_{h}'
    sub = df[mask & df[col].notna()]
    if sub.empty:
        return pd.Series(dtype=float), sub
    g = sub.groupby('date')
    per_date = g['excess_' + str(h)].mean()
    counts = g.size()
    return per_date[counts >= 1], sub


def summarize(df, mask, h, split_date):
    col = f'fwd_{h}'
    ex = f'excess_{h}'
    out = {}
    for label, part in (('前半', df['date'] < split_date), ('後半', df['date'] >= split_date), ('全期間', None)):
        m = mask & df[col].notna()
        if part is not None:
            m &= part
        sub = df[m]
        if sub.empty:
            out[label] = {'n': 0}
            continue
        per_date = sub.groupby('date')[ex].mean()
        names = sub.groupby('date').size()
        step = max(1, h // PANEL_STEP)
        sample = per_date.iloc[::step]
        t = None
        if len(sample) >= 3 and sample.std(ddof=1) > 0:
            t = float(sample.mean() / (sample.std(ddof=1) / np.sqrt(len(sample))))
        out[label] = {
            'n': int(len(sub)), 'dates': int(per_date.size), 'avg_names_per_date': round(float(names.mean()), 1),
            'mean_return_pct': round(float(sub[col].mean() * 100), 2),
            'win_rate_pct': round(float((sub[col] > 0).mean() * 100), 1),
            'excess_pct': round(float(sub[ex].mean() * 100), 2),
            'excess_win_rate_pct': round(float((sub[ex] > 0).mean() * 100), 1),
            'date_mean_excess_pct': round(float(per_date.mean() * 100), 2),
            'share_of_dates_beating_market_pct': round(float((per_date > 0).mean() * 100), 1),
            't_stat_nonoverlap': round(t, 2) if t is not None else None,
        }
    return out


def main():
    panel_path = os.path.join(OUT_DIR, sys.argv[1] if len(sys.argv) > 1 else 'panel.pkl')
    raw = pd.read_pickle(panel_path)
    df = raw[raw['tradeable']].copy()
    df = add_cross_sectional_ranks(df)
    for h in HORIZONS:
        col = f'fwd_{h}'
        df[f'excess_{h}'] = df[col] - df.groupby('date')[col].transform('mean')
    dates = sorted(df['date'].unique())
    split_date = dates[len(dates) // 2]
    print(f'[styles] 取引可能な銘柄×日 {len(df):,}（除外 {len(raw) - len(df):,}）/ 後半の開始日 {split_date}', flush=True)

    report = {'meta': repro.run_metadata(sorted(raw['ticker'].unique()),
                                         {'horizons': HORIZONS, 'min_names_per_date': MIN_NAMES_PER_DATE,
                                          'split_date': split_date, 'universe_filter': filter_params(),
                                          'panel': os.path.basename(panel_path)},
                                         ['evaluate_investor_styles.py', 'investor_models.py', 'build_panel.py',
                                          'fundamental_features.py', 'universe_filters.py']),
              'period': [dates[0], dates[-1]], 'split_date': split_date, 'models': [], 'condition_search': {}}

    for model in MODELS:
        passed, score, conds = evaluate_model(df, model)
        entry = {k: model[k] for k in ('type', 'investor', 'style_label', 'summary', 'omitted')}
        entry['conditions'] = [{'label': c['label'], 'core': c['core']} for c in model['conditions']]
        entry['pass'] = {str(h): summarize(df, passed, h, split_date) for h in HORIZONS}
        # スコア5分位（同じ日の判定可能な銘柄の中で）
        q = pd.Series(np.nan, index=df.index)
        valid = score.notna()
        q[valid] = df[valid].assign(_s=score[valid]).groupby('date')['_s'].rank(pct=True, method='first')
        buckets = {}
        for k, (lo, hi) in enumerate([(0, .2), (.2, .4), (.4, .6), (.6, .8), (.8, 1.0001)], start=1):
            m = valid & (q > lo) & (q <= hi) if k > 1 else valid & (q <= hi)
            buckets[f'Q{k}'] = {str(h): summarize(df, m, h, split_date)['全期間'] for h in HORIZONS}
        entry['score_quintiles'] = buckets
        report['models'].append(entry)
        p60 = entry['pass']['60']
        print(f"[styles] {model['type']} {model['investor']}: 合格 平均{p60['全期間'].get('avg_names_per_date')}銘柄/日 "
              f"60日超過 前半{p60['前半'].get('excess_pct')}% 後半{p60['後半'].get('excess_pct')}%", flush=True)

    # --- 条件探索（タイプごと、単独条件＋2条件の組み合わせ） ---
    by_type = {}
    for model in MODELS:
        for c in model['conditions']:
            by_type.setdefault(model['type'], {})[c['label']] = c['fn']
    total_tests = 0
    for typ, cond_fns in by_type.items():
        values = {label: fn(df) == 1.0 for label, fn in cond_fns.items()}
        candidates = [(label,) for label in values] + list(itertools.combinations(sorted(values), 2))
        results = []
        for combo in candidates:
            mask = np.logical_and.reduce([values[c] for c in combo])
            mask = pd.Series(mask, index=df.index)
            s60 = summarize(df, mask, 60, split_date)
            s20 = summarize(df, mask, 20, split_date)
            f, b = s60['前半'], s60['後半']
            if not f.get('n') or not b.get('n') or f['avg_names_per_date'] < MIN_NAMES_PER_DATE or \
                    b['avg_names_per_date'] < MIN_NAMES_PER_DATE or f['dates'] < MIN_DATES or b['dates'] < MIN_DATES:
                continue
            total_tests += 1
            results.append({'conditions': list(combo), '60日': s60, '20日': s20,
                            'robust': f['excess_pct'] > 0 and b['excess_pct'] > 0,
                            'min_half_excess_60': min(f['excess_pct'], b['excess_pct'])})
        chosen_in_first = sorted(results, key=lambda r: (-r['60日']['前半']['excess_pct'], r['conditions']))[:10]
        robust = sorted([r for r in results if r['robust']], key=lambda r: (-r['min_half_excess_60'], r['conditions']))[:10]
        report['condition_search'][typ] = {'tested': len(results), 'top_by_first_half': chosen_in_first,
                                           'robust_both_halves': robust}
    report['condition_search_total_tests'] = total_tests

    with open(os.path.join(OUT_DIR, 'investor_styles_eval.json'), 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    with open(os.path.join(OUT_DIR, 'investor_styles_eval.md'), 'w', encoding='utf-8') as f:
        f.write(render_markdown(report))
    print('[styles] 完了', flush=True)


def _fmt(s):
    if not s or not s.get('n'):
        return '— | — | — | —'
    t = s.get('t_stat_nonoverlap')
    return f"{s['avg_names_per_date']} | {s['win_rate_pct']}% | {s['excess_pct']:+.2f}% | {'' if t is None else t}"


def render_markdown(rep):
    meta = rep['meta']
    code = meta.get('code') or {}
    lines = ['# 投資家タイプ別モデルの検証（先読みなし）', '',
             f"- 期間: {rep['period'][0]}〜{rep['period'][1]}（5営業日ごと）。後半の開始日: {rep['split_date']}",
             '- 対象: その日に取引可能な銘柄（売買代金不足・仕手株化を除外、株価の上限なし）。上場廃止銘柄を含む。',
             '- 成績: 翌営業日の始値→20/60営業日後の終値。超過＝同じ日の取引可能な全銘柄平均との差。',
             '- t値: 保有期間が重ならない日付だけで計算（±2を超えると偶然とは考えにくい目安）。',
             f"- 条件探索で調べた組み合わせ数: {rep['condition_search_total_tests']}（数が多いほど偶然の当たりが混ざるので、前半・後半の両方で効いているかを重視）",
             f"- 再現性: データ指紋 {meta.get('data_fingerprint_sha256', '')[:16]} / コード {str(code.get('git_commit'))[:8]}"
             f"{'（未コミットの変更あり）' if code.get('git_dirty') else ''}", '']
    current = None
    for m in rep['models']:
        if m['type'] != current:
            current = m['type']
            lines += [f'## {current}', '']
        lines += [f"### {m['investor']}（投稿での呼び方：{m['style_label']}）", '', m['summary'], '',
                  '条件（◎＝必須、○＝スコアのみ）: ' + ' / '.join(('◎' if c['core'] else '○') + c['label'] for c in m['conditions']),
                  '', f"データの制約で省略・代替した点: {m['omitted']}", '',
                  '| 保有 | 期間 | 合格銘柄/日 | 勝率 | 超過 | t値 |', '|---|---|---:|---:|---:|---:|']
        for h in ('20', '60'):
            for part in ('前半', '後半'):
                lines.append(f"| {h}日 | {part} | {_fmt(m['pass'][h][part])} |")
        q = m['score_quintiles']
        lines += ['', 'スコア5分位（Q5＝スコア上位20%）の60日超過: ' +
                  ' / '.join(f"{k} {v['60'].get('excess_pct', '—')}%" for k, v in q.items()), '']
    lines += ['## 勝ちが出やすい条件（前半・後半の両方で市場平均を上回ったもの、60日保有）', '']
    for typ, res in rep['condition_search'].items():
        lines += [f'### {typ}（{res["tested"]}通りを検証）', '',
                  '| 条件 | 銘柄/日 | 前半 超過 | 後半 超過 | 後半 勝率 | 後半 t値 | 20日保有 後半 超過 |', '|---|---:|---:|---:|---:|---:|---:|']
        if not res['robust_both_halves']:
            lines.append('| （前半・後半の両方で市場平均を上回った条件なし） | | | | | | |')
        for r in res['robust_both_halves']:
            f, b = r['60日']['前半'], r['60日']['後半']
            lines.append(f"| {' ＋ '.join(r['conditions'])} | {r['60日']['全期間']['avg_names_per_date']} | "
                         f"{f['excess_pct']:+.2f}% | {b['excess_pct']:+.2f}% | {b['win_rate_pct']}% | "
                         f"{b.get('t_stat_nonoverlap')} | {r['20日']['後半'].get('excess_pct', '—')}% |")
        lines.append('')
    return '\n'.join(lines)


if __name__ == '__main__':
    sys.exit(main())
