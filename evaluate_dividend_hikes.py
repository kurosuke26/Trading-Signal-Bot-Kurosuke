#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
evaluate_dividend_hikes.py — 増配の発表を「仕込めるタイミング」と「事前の兆候」の両面から調べる（2026-09-16追加）。

【増配イベントの定義】（build_panel.py が開示日順に作った配当予想の変化 dividend_events.pkl から）
- 期初増配予想：FY決算短信で出た翌期の年間配当予想が、その期の実績配当を上回る
- 増配修正：同じ期の年間配当予想が、それまでの予想から引き上げられた（決算短信・配当予想の修正の開示）
- 初配・復配：前期の実績配当が0で、予想配当がプラス
同じ銘柄で20営業日以内に続いた増配は、最初の1件にまとめる。1株あたりの値は開示時点の株数ベースで比較。

【問い】
Q1. 仕込めるタイミングはどれくらいあったか：増配の発表は月に何件あり、発表の20日前・60日前に買っていたら
    発表日まで／発表後60日まででどれだけ上がったか（同じ日の非増配銘柄との差）。発表を見てから買った場合も比較。
Q2. 発表前のスクリーニング時点の兆候：発表の前（開示日より前の直近パネル日）の特徴量を、同じ日の増配しなかった
    配当銘柄と比べる。前半期間で「増配の起きやすさ」に効いた特徴を選び、後半期間で本当に増配が多かったか確かめる。
Q3. 配当利回りの水準：増配前の予想配当利回りの分布と、利回り帯ごとの増配率・その後の上昇率。

【先読み】特徴量は開示日より前のパネル日の値だけ。増配が起きたかどうか（ラベル）と将来リターンは、
「結果」として集計にだけ使う。

【出力】data/backtest_out/dividend_hikes_eval.json / .md
"""

import json
import os
import sys

import numpy as np
import pandas as pd

import repro
from universe_filters import filter_params

OUT_DIR = 'data/backtest_out'
LABEL_WINDOW_DAYS = 90   # パネル日から何暦日以内に増配が発表されたら「増配あり」とみなすか
DEDUP_TRADING_DAYS = 20
FEATURES = [
    ('div_yield_forecast', '予想配当利回り(%)'), ('payout_forecast', '予想配当性向'),
    ('op_progress_excess', '営業利益の決算進捗（予想比の上振れ）'), ('op_revision_from_initial', '営業利益予想の期初からの修正率'),
    ('eps_revision_from_initial', 'EPS予想の期初からの修正率'), ('eps_growth_forecast', '予想EPS成長率'),
    ('op_growth_forecast', '予想営業利益成長率'), ('op_yoy', '直近四半期の営業利益 前年同期比'),
    ('sales_yoy', '直近四半期の売上 前年同期比'), ('roe', 'ROE'), ('equity_ratio', '自己資本比率'),
    ('cash_to_mktcap', '現金÷時価総額'), ('pbr', 'PBR'), ('per_forecast', '予想PER'),
    ('ret_60', '過去60日の株価上昇率'), ('high_52w_ratio', '株価÷52週高値'), ('mktcap_oku', '時価総額(億円)'),
    ('div_growth_forecast', '予想配当の前期比'),
]


def classify_events(ev):
    ev = ev.copy()
    kind = pd.Series(None, index=ev.index, dtype=object)
    pa, pf, nd = ev['prev_actual'], ev['prev_forecast'], ev['new_div']
    first_guidance = ev['source'].str.contains('翌期予想') & pf.isna()
    kind[first_guidance & (pa > 0) & (nd > pa * 1.001)] = '期初増配予想'
    kind[first_guidance & (pa > 0) & (nd < pa * 0.999)] = '期初減配予想'
    kind[pf.notna() & (pf > 0) & (nd > pf * 1.001)] = '増配修正'
    kind[pf.notna() & (pf > 0) & (nd < pf * 0.999)] = '減配修正'
    kind[(pa == 0) & (nd > 0) & kind.isna()] = '初配・復配'
    ev['kind'] = kind
    ev['change_pct'] = np.where(pf.notna() & (pf > 0), nd / pf - 1, np.where(pa > 0, nd / pa - 1, np.nan)) * 100
    return ev[ev['kind'].notna()]


def dedup(events, trading_dates):
    pos = {d: k for k, d in enumerate(trading_dates)}
    out, last = [], {}
    for _, e in events.sort_values(['ticker', 'disc_date']).iterrows():
        k = pos.get(e['disc_date'])
        if k is None:
            k = int(np.searchsorted(trading_dates, e['disc_date']))
        if e['ticker'] in last and k - last[e['ticker']] < DEDUP_TRADING_DAYS:
            continue
        last[e['ticker']] = k
        out.append(e)
    return pd.DataFrame(out)


def main():
    panel = pd.read_pickle(os.path.join(OUT_DIR, 'panel.pkl'))
    events = pd.read_pickle(os.path.join(OUT_DIR, 'dividend_events.pkl'))
    daily = pd.read_pickle(os.path.join(OUT_DIR, 'daily.pkl'))
    trading_dates = sorted(daily['date'].unique())
    start, end = panel['date'].min(), panel['date'].max()

    ev = classify_events(events)
    ev = ev[(ev['disc_date'] >= start) & (ev['disc_date'] <= end)]
    hikes = dedup(ev[ev['kind'].isin(['期初増配予想', '増配修正', '初配・復配'])], trading_dates)
    cuts = dedup(ev[ev['kind'].isin(['期初減配予想', '減配修正'])], trading_dates)
    print(f'[hikes] 増配 {len(hikes):,} 件 / 減配 {len(cuts):,} 件（{start}〜{end}）', flush=True)

    # --- 日次価格（発表前後のリターン計算用、調整済み株価） ---
    px = daily.pivot(index='date', columns='ticker', values='close').sort_index()
    op = daily.pivot(index='date', columns='ticker', values='open').sort_index()
    tradeable = daily.pivot(index='date', columns='ticker', values='tradeable').sort_index()
    didx = {d: k for k, d in enumerate(px.index)}

    def window_return(ticker, d0_idx, d1_idx, entry_open=True):
        if ticker not in px.columns or d0_idx < 0 or d1_idx >= len(px.index) or d0_idx >= d1_idx:
            return None
        entry = op[ticker].iat[d0_idx] if entry_open else px[ticker].iat[d0_idx]
        exitp = px[ticker].iat[d1_idx]
        if not (entry == entry and exitp == exitp and entry > 0):
            return None
        return exitp / entry - 1

    market_ret_cache = {}

    def market_return(d0_idx, d1_idx):
        key = (d0_idx, d1_idx)
        if key not in market_ret_cache:
            if d0_idx < 0 or d1_idx >= len(px.index):
                market_ret_cache[key] = None
            else:
                mask = tradeable.iloc[d0_idx].fillna(False).astype(bool)
                r = (px.iloc[d1_idx][mask] / op.iloc[d0_idx][mask] - 1).replace([np.inf, -np.inf], np.nan).dropna()
                market_ret_cache[key] = float(r.mean()) if len(r) else None
        return market_ret_cache[key]

    # --- Q1：タイミング ---
    rows = []
    for _, e in hikes.iterrows():
        k = int(np.searchsorted(px.index.values, e['disc_date']))  # 開示日（またはその次の営業日）
        if k >= len(px.index):
            continue
        rec = {'ticker': e['ticker'], 'disc_date': e['disc_date'], 'kind': e['kind'], 'change_pct': e['change_pct']}
        was_tradeable = bool(tradeable[e['ticker']].iat[max(0, k - 1)]) if e['ticker'] in tradeable.columns else False
        rec['tradeable_before'] = was_tradeable
        for label, d0, d1 in (('60日前に買い→発表日', k - 60, k), ('20日前に買い→発表日', k - 20, k),
                              ('20日前に買い→発表後60日', k - 20, k + 60),
                              ('発表翌日に買い→20日後', k + 1, k + 21), ('発表翌日に買い→60日後', k + 1, k + 61)):
            r = window_return(e['ticker'], d0, d1)
            m = market_return(d0, d1)
            rec[label] = r
            rec[label + '（超過）'] = (r - m) if r is not None and m is not None else None
        rows.append(rec)
    timing = pd.DataFrame(rows)
    timing_t = timing[timing['tradeable_before']]

    def agg(df):
        out = {'件数': int(len(df))}
        for col in ('60日前に買い→発表日', '20日前に買い→発表日', '20日前に買い→発表後60日',
                    '発表翌日に買い→20日後', '発表翌日に買い→60日後'):
            s = df[col].dropna()
            sx = df[col + '（超過）'].dropna()
            out[col] = {'n': int(len(s)), '平均上昇率%': round(s.mean() * 100, 2) if len(s) else None,
                        '勝率%': round((s > 0).mean() * 100, 1) if len(s) else None,
                        '平均超過%': round(sx.mean() * 100, 2) if len(sx) else None,
                        '超過勝率%': round((sx > 0).mean() * 100, 1) if len(sx) else None}
        return out

    monthly = timing_t.assign(month=timing_t['disc_date'].str[:7]).groupby('month').size().to_dict()
    q1 = {'全増配（取引可能だった銘柄）': agg(timing_t), '種類別': {k: agg(g) for k, g in timing_t.groupby('kind')},
          '月別件数': monthly}

    # --- Q2：事前の兆候（パネル日→90日以内に増配が発表されたか） ---
    p = panel[panel['tradeable'] & (panel['div_yield_forecast'] > 0)].copy()
    hk = hikes[['ticker', 'disc_date']].sort_values('disc_date')
    by_t = {t: g['disc_date'].tolist() for t, g in hk.groupby('ticker')}

    def label(row):
        lst = by_t.get(row.ticker)
        if not lst:
            return 0
        lim = str(np.datetime64(row.date) + np.timedelta64(LABEL_WINDOW_DAYS, 'D'))
        return int(any(row.date < d <= lim for d in lst))

    p['hike_next'] = [label(r) for r in p[['ticker', 'date']].itertuples(index=False)]
    # ラベル期間がデータ末尾を超える日は除外（増配の有無が判定できないため）
    last_ok = str(np.datetime64(end) - np.timedelta64(LABEL_WINDOW_DAYS, 'D'))
    p = p[p['date'] <= last_ok]
    dates = sorted(p['date'].unique())
    split = dates[len(dates) // 2]
    base_first = p[p['date'] < split]['hike_next'].mean()
    base_second = p[p['date'] >= split]['hike_next'].mean()

    lifts = []
    for col, name in FEATURES:
        sub = p[p[col].notna()].copy()
        if len(sub) < 1000:
            continue
        sub_first, sub_second = sub[sub['date'] < split], sub[sub['date'] >= split]
        if len(sub_first) < 500 or len(sub_second) < 500:
            continue  # 前半・後半のどちらかでデータが少ない特徴量（例：前年同期比は前半に値が無い）は比較しない
        sub_base_first, sub_base_second = sub_first['hike_next'].mean(), sub_second['hike_next'].mean()
        sub['q'] = sub.groupby('date')[col].rank(pct=True)
        sub['bucket'] = pd.cut(sub['q'], [0, .2, .4, .6, .8, 1.0], labels=['Q1', 'Q2', 'Q3', 'Q4', 'Q5'],
                               include_lowest=True)
        tab = {}
        for b, g in sub.groupby('bucket', observed=True):
            f, s = g[g['date'] < split], g[g['date'] >= split]
            tab[str(b)] = {'値の中央値': round(float(g[col].median()), 4),
                           '増配率_前半%': round(f['hike_next'].mean() * 100, 2) if len(f) else None,
                           '増配率_後半%': round(s['hike_next'].mean() * 100, 2) if len(s) else None}
        first_rates = {b: v['増配率_前半%'] for b, v in tab.items() if v['増配率_前半%'] is not None}
        best = max(first_rates, key=first_rates.get) if first_rates else None
        med_hike = sub[sub['hike_next'] == 1][col].median()
        med_none = sub[sub['hike_next'] == 0][col].median()
        lifts.append({'feature': col, 'name': name, 'quintiles': tab, 'best_bucket_first_half': best,
                      'lift_first': round(first_rates[best] / (sub_base_first * 100), 2) if best else None,
                      'lift_second_same_bucket': round(tab[best]['増配率_後半%'] / (sub_base_second * 100), 2)
                      if best and tab[best]['増配率_後半%'] is not None else None,
                      'median_hike': round(float(med_hike), 4) if med_hike == med_hike else None,
                      'median_no_hike': round(float(med_none), 4) if med_none == med_none else None})
    lifts.sort(key=lambda x: -(x['lift_first'] or 0))

    # 前半で選んだ上位3特徴の「最も増配が多かった5分位」を2つ以上満たす → 後半で確かめる（プロスペクティブ）
    top = [x for x in lifts if x['best_bucket_first_half'] and (x['lift_first'] or 0) > 1.1][:3]
    rule = None
    if top:
        hits = pd.Series(0, index=p.index)
        for x in top:
            q = p.groupby('date')[x['feature']].rank(pct=True)
            lo = {'Q1': 0, 'Q2': .2, 'Q3': .4, 'Q4': .6, 'Q5': .8}[x['best_bucket_first_half']]
            hits += ((q > lo) & (q <= lo + .2)).astype(int) if lo > 0 else (q <= .2).astype(int)
        sel = hits >= 2
        second = p['date'] >= split
        p['excess_60'] = p['fwd_60'] - p.groupby('date')['fwd_60'].transform('mean')
        rule = {'features': [(x['name'], x['best_bucket_first_half']) for x in top],
                '後半_ルール該当の増配率%': round(p[second & sel]['hike_next'].mean() * 100, 2),
                '後半_全体の増配率%': round(p[second]['hike_next'].mean() * 100, 2),
                '後半_ルール該当の銘柄/日': round(float(p[second & sel].groupby('date').size().mean()), 1),
                '後半_ルール該当の60日超過%': round(float(p[second & sel]['excess_60'].mean() * 100), 2),
                '前半_ルール該当の60日超過%': round(float(p[~second & sel]['excess_60'].mean() * 100), 2)}

    # --- Q3：配当利回りの水準 ---
    bins = [0, 2, 3, 3.5, 4, 5, 100]
    labels = ['〜2%', '2〜3%', '3〜3.5%', '3.5〜4%', '4〜5%', '5%〜']
    p['yield_band'] = pd.cut(p['div_yield_forecast'], bins, labels=labels, right=False)
    p['excess_60'] = p['fwd_60'] - p.groupby('date')['fwd_60'].transform('mean')
    q3 = {}
    for b, g in p.groupby('yield_band', observed=True):
        q3[str(b)] = {'銘柄×日': int(len(g)), '90日以内の増配率%': round(g['hike_next'].mean() * 100, 2),
                      '増配した銘柄の60日超過%': round(float(g[g['hike_next'] == 1]['excess_60'].mean() * 100), 2),
                      '増配しなかった銘柄の60日超過%': round(float(g[g['hike_next'] == 0]['excess_60'].mean() * 100), 2)}
    pre_yield = p[p['hike_next'] == 1]['div_yield_forecast']
    q3_dist = {f'{int(q * 100)}%点': round(float(pre_yield.quantile(q)), 2) for q in (.1, .25, .5, .75, .9)}

    report = {'meta': repro.run_metadata(sorted(panel['ticker'].unique()),
                                         {'label_window_days': LABEL_WINDOW_DAYS, 'dedup_trading_days': DEDUP_TRADING_DAYS,
                                          'universe_filter': filter_params(), 'split_date': split},
                                         ['evaluate_dividend_hikes.py', 'build_panel.py', 'fundamental_features.py',
                                          'universe_filters.py']),
              'period': [start, end], 'hike_events': int(len(hikes)), 'cut_events': int(len(cuts)),
              'hike_kinds': hikes['kind'].value_counts().to_dict(), 'Q1_timing': q1,
              'Q2_base_rate_pct': {'前半': round(base_first * 100, 2), '後半': round(base_second * 100, 2)},
              'Q2_features': lifts, 'Q2_prospective_rule': rule, 'Q3_yield_bands': q3,
              'Q3_pre_hike_yield_distribution': q3_dist, 'split_date': split}
    with open(os.path.join(OUT_DIR, 'dividend_hikes_eval.json'), 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    with open(os.path.join(OUT_DIR, 'dividend_hikes_eval.md'), 'w', encoding='utf-8') as f:
        f.write(render_markdown(report))
    print('[hikes] 完了', flush=True)


def render_markdown(r):
    lines = ['# 増配の検証（仕込みのタイミング・事前の兆候・配当利回り）', '',
             f"- 期間: {r['period'][0]}〜{r['period'][1]} / 増配 {r['hike_events']:,}件（{r['hike_kinds']}）/ 減配 {r['cut_events']:,}件",
             '- 対象は発表前日に取引可能だった銘柄（売買代金不足・仕手株化を除外）。株価は分割調整済み。', '',
             '## Q1. 仕込めるタイミング', '',
             '| 買い方 | 件数 | 平均上昇率 | 勝率 | 市場平均との差 | 差の勝率 |', '|---|---:|---:|---:|---:|---:|']
    for col, v in r['Q1_timing']['全増配（取引可能だった銘柄）'].items():
        if col == '件数':
            continue
        lines.append(f"| {col} | {v['n']:,} | {v['平均上昇率%']}% | {v['勝率%']}% | {v['平均超過%']}% | {v['超過勝率%']}% |")
    lines += ['', '月別の増配発表件数: ' + ' / '.join(f'{m} {n}' for m, n in r['Q1_timing']['月別件数'].items()), '',
              '## Q2. 発表前のスクリーニング時点の兆候', '',
              f"90日以内に増配が発表された割合（全体）: 前半 {r['Q2_base_rate_pct']['前半']}% / 後半 {r['Q2_base_rate_pct']['後半']}%", '',
              '| 特徴量 | 増配した銘柄の中央値 | しなかった銘柄の中央値 | 前半で最も増配が多かった5分位 | 前半の倍率 | 後半の同じ5分位の倍率 |',
              '|---|---:|---:|---|---:|---:|']
    for x in r['Q2_features']:
        lines.append(f"| {x['name']} | {x['median_hike']} | {x['median_no_hike']} | {x['best_bucket_first_half']} | "
                     f"{x['lift_first']} | {x['lift_second_same_bucket']} |")
    if r['Q2_prospective_rule']:
        lines += ['', '前半で選んだ兆候（上位3つのうち2つ以上）を後半に当てはめた結果: ' +
                  json.dumps(r['Q2_prospective_rule'], ensure_ascii=False)]
    lines += ['', '## Q3. 配当利回りの水準', '',
              '増配した銘柄の、発表前の予想配当利回り: ' + ' / '.join(f'{k} {v}%' for k, v in r['Q3_pre_hike_yield_distribution'].items()),
              '', '| 予想配当利回り | 銘柄×日 | 90日以内の増配率 | 増配した銘柄の60日超過 | しなかった銘柄の60日超過 |', '|---|---:|---:|---:|---:|']
    for b, v in r['Q3_yield_bands'].items():
        lines.append(f"| {b} | {v['銘柄×日']:,} | {v['90日以内の増配率%']}% | {v['増配した銘柄の60日超過%']}% | {v['増配しなかった銘柄の60日超過%']}% |")
    return '\n'.join(lines)


if __name__ == '__main__':
    sys.exit(main())
