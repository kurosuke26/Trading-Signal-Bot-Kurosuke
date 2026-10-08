#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
evaluate_roman.py — ロマン枠（10倍株の候補、value_growth_scoring.py の ROMAN）の「精度」を先読みなしで測る
（2026-10-08追加。分析専用）。

【何を測るか】
手元のデータは約2年分しかなく、10倍になるには通常何年もかかるため「10倍株を当てた割合」は測れない。
代わりに、候補に選ばれた銘柄が、その後 H営業日（120日≒半年／250日≒1年）の間に
  ・最高値で1.5倍／2倍／3倍に達した割合（大化けの兆しを捕まえられているか）
  ・H日後の騰落率（持ち続けた場合）
を、同じ日の「同じ時価総額帯（50〜1,500億円）の取引可能な全銘柄」と比べる。比率（リフト）が1を大きく
超えていれば、大化けしやすい銘柄を選べていることになる。

【先読みを防ぐルール】
- 候補はパネル（build_panel.py、5営業日ごと）の t日の引けまでの情報だけで選ぶ（決算は開示日がt日以前のもの、
  時価総額はt日のもの、順位は同じ日の取引可能銘柄の中だけ）。
- 値動きは t+1日の始値を起点に、その後の終値（分割調整済み）で測る。上場廃止した銘柄は最後の取引日まで。
- 同じ銘柄が何度も選ばれると成績が水増しされるため、「初めて候補に入った日」だけを数える集計も出す。
- 条件（時価総額帯・成長率・予想PER・ROE）は検証の前に決めたもの（調査記事・論文の条件をもとに設定）で、
  この検証の結果を見て調整していない。

【出力】data/backtest_out/roman_eval.json / .md
"""

import json
import os
import sys

import numpy as np
import pandas as pd

from evaluate_value_growth import OUT, SPLIT_DATE, SUFFIX, load_panel, score_panel
from value_growth_scoring import ROMAN_MKTCAP_OKU

HORIZONS = (120, 250)
TOP_N = 10
LEVELS = (1.5, 2.0, 3.0)


def forward_paths(daily):
    close = daily.pivot(index='date', columns='ticker', values='close').sort_index()
    opn = daily.pivot(index='date', columns='ticker', values='open').sort_index()
    return close, opn


def measure(close, opn, picks, h):
    """picks: DataFrame[ticker, date]。戻り値: DataFrame[ticker, date, max_mult, end_ret]（期間が足りないものは除外）"""
    dates = list(close.index)
    pos = {d: i for i, d in enumerate(dates)}
    C = close.to_numpy()
    O = opn.to_numpy()
    col = {t: j for j, t in enumerate(close.columns)}
    out = []
    for t, d in zip(picks['ticker'], picks['date']):
        i, j = pos.get(d), col.get(t)
        if i is None or j is None or i + h >= len(dates):
            continue  # その後 h 営業日のデータが無い（期間末）
        entry = O[i + 1, j]
        if not entry == entry or entry <= 0:
            continue
        path = C[i + 1:i + 1 + h, j]
        path = path[~np.isnan(path)]
        if not len(path):
            continue
        out.append((t, d, float(path.max() / entry), float(path[-1] / entry - 1)))  # 上場廃止なら最後の取引日
    return pd.DataFrame(out, columns=['ticker', 'date', 'max_mult', 'end_ret'])


def summarize(m, base):
    if m.empty:
        return {'n': 0}
    r = {'n': int(len(m)), 'unique_tickers': int(m['ticker'].nunique()),
         'end_ret_mean_pct': round(m['end_ret'].mean() * 100, 2), 'end_ret_median_pct': round(m['end_ret'].median() * 100, 2)}
    bm = base.groupby('date')['end_ret'].mean()
    ex = m['end_ret'] - m['date'].map(bm)
    r['excess_vs_band_pct'] = round(float(ex.mean()) * 100, 2)
    for lv in LEVELS:
        hit = float((m['max_mult'] >= lv).mean())
        # 同じ日の同じ時価総額帯の全銘柄の到達率（日付の構成を揃えるため、選ばれた日の基準値を平均）
        bh = base.assign(h=base['max_mult'] >= lv).groupby('date')['h'].mean()
        b = float(m['date'].map(bh).mean())
        r[f'hit_{lv}x_pct'] = round(hit * 100, 1)
        r[f'base_{lv}x_pct'] = round(b * 100, 1)
        r[f'lift_{lv}x'] = round(hit / b, 2) if b > 0 else None
    return r


def main():
    p = load_panel()
    s = score_panel(p)
    band = (s['mktcap_oku'] >= ROMAN_MKTCAP_OKU[0]) & (s['mktcap_oku'] <= ROMAN_MKTCAP_OKU[1])
    cand = s[s['roman_gate'] & s['roman_score'].notna()].sort_values(['date', 'roman_score', 'ticker'],
                                                                     ascending=[True, False, True])
    sets = {
        f'ロマン枠 上位{TOP_N}': cand.groupby('date').head(TOP_N)[['ticker', 'date']],
        'ロマン枠 入口の条件を通った全銘柄': cand[['ticker', 'date']],
    }
    first = cand.groupby('date').head(TOP_N).sort_values('date').drop_duplicates('ticker')[['ticker', 'date']]
    sets[f'ロマン枠 上位{TOP_N}（初めて入った日だけ）'] = first
    # 【v2】前半だけで「その後半年で2倍になった銘柄が、事前に持っていた特徴」を調べて決めた条件
    # （時価総額の小ささ・会社予想の上方修正・120日の上昇・業種内の勢い）。後半はそのまま当てはめて確かめる。
    c2 = s[s['roman2_gate'] & s['roman2_score'].notna()].sort_values(['date', 'roman2_score', 'ticker'],
                                                                     ascending=[True, False, True])
    sets[f'ロマン枠v2 上位{TOP_N}'] = c2.groupby('date').head(TOP_N)[['ticker', 'date']]
    sets[f'ロマン枠v2 上位{TOP_N}（初めて入った日だけ）'] = (c2.groupby('date').head(TOP_N).sort_values('date')
                                                     .drop_duplicates('ticker')[['ticker', 'date']])
    sets['ロマン枠v2 入口の条件を通った全銘柄'] = c2[['ticker', 'date']]
    base_picks = s[band][['ticker', 'date']]

    close, opn = forward_paths(pd.read_pickle(os.path.join(OUT, f'daily{SUFFIX}.pkl')))
    rep = {'horizons': HORIZONS, 'top_n': TOP_N, 'split': SPLIT_DATE, 'mktcap_band_oku': ROMAN_MKTCAP_OKU,
           'candidates_per_date': round(float(cand.groupby('date').size().mean()), 1), 'results': {}}
    for h in HORIZONS:
        base = measure(close, opn, base_picks, h)
        for name, picks in sets.items():
            m = measure(close, opn, picks, h)
            rep['results'][f'{name}｜{h}営業日'] = {
                '全期間': summarize(m, base),
                '前半': summarize(m[m['date'] < SPLIT_DATE], base),
                '後半': summarize(m[m['date'] >= SPLIT_DATE], base),
            }
        # 参考：期間中に実際に大化けした銘柄のうち、事前に候補に入っていた割合（取りこぼし）
        winners = base[base['max_mult'] >= 2.0]
        caught = winners.merge(cand[['ticker', 'date']], on=['ticker', 'date'])
        rep['results'][f'参考：{h}営業日で2倍以上になった銘柄×日のうち、その日に入口を通っていた割合'] = {
            '全期間': {'n': int(len(winners)), 'caught_pct': round(len(caught) / len(winners) * 100, 1) if len(winners) else None}}

    with open(os.path.join(OUT, 'roman_eval.json'), 'w', encoding='utf-8') as f:
        json.dump(rep, f, ensure_ascii=False, indent=2)
    with open(os.path.join(OUT, 'roman_eval.md'), 'w', encoding='utf-8') as f:
        f.write(render(rep))
    print(render(rep))


def render(rep):
    L = ['# ロマン枠（10倍株の候補）の精度（先読みなし）', '',
         f"- 候補：時価総額{rep['mktcap_band_oku'][0]}〜{rep['mktcap_band_oku'][1]}億円の成長株（条件は value_growth_scoring.py）。"
         f"入口を通る銘柄は1日平均{rep['candidates_per_date']}件",
         '- 到達率＝その後の期間中に最高値が1.5倍／2倍／3倍に達した割合。基準＝同じ日・同じ時価総額帯の取引可能な全銘柄。リフト＝到達率÷基準',
         f"- 前半＝〜{rep['split']}の前日、後半＝{rep['split']}〜（250営業日は期間の都合で前半のみ）", '',
         '| 対象 | 期間 | 件数 | 銘柄数 | 1.5倍到達 | 基準 | 2倍到達 | 基準 | リフト2倍 | 3倍到達 | 基準 | 期末騰落（平均） | 時価総額帯との差 |',
         '|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    for name, parts in rep['results'].items():
        for part, x in parts.items():
            if 'caught_pct' in x:
                L.append(f"| {name} | {part} | {x['n']:,} | | | | 取り込めていた割合 {x['caught_pct']}% | | | | | | |")
                continue
            if not x.get('n'):
                continue
            L.append(f"| {name} | {part} | {x['n']:,} | {x['unique_tickers']} | {x['hit_1.5x_pct']}% | {x['base_1.5x_pct']}% | "
                     f"{x['hit_2.0x_pct']}% | {x['base_2.0x_pct']}% | {x['lift_2.0x']} | {x['hit_3.0x_pct']}% | {x['base_3.0x_pct']}% | "
                     f"{x['end_ret_mean_pct']:+.1f}% | {x['excess_vs_band_pct']:+.1f}% |")
    return '\n'.join(L)


if __name__ == '__main__':
    sys.exit(main())
