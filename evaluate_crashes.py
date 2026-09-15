#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
evaluate_crashes.py — 「○○ショック」のような暴落の後に買うと有利か、を2つの面から検証する（2026-09-16追加）。

【市場全体の指数】
daily.pkl（全銘柄の日次・分割調整済み）から、前日時点で取引可能だった銘柄の日次騰落率の平均（等金額）と、
前日の時価総額で加重した平均（時価総額加重）を作り、それぞれ累積して指数にする。

【A. プロスペクティブ（その時点で分かる情報だけで決めたルール）】
- トリガー（t日の引けで判定）：指数の1日の下落率が −3% / −4% / −5% 以下、または
  指数が直近20営業日の高値から −7% / −10% / −15% 以下まで下落。
  同じトリガーは、発動後20営業日は再発動しない（同じ暴落を二重に数えない）。
- 買い方：t+1+N日の始値で指数（全銘柄等金額）を買い、t+N+H日の終値で売る（N=0,1,2,3,5,10、H=5,20,60）
- 比べる相手：同じN・Hで「すべての日」に買った場合。有意差は、イベントと同じ数の日付をランダムに選ぶ試行を
  10,000回行い（乱数の種を固定＝再現可能）、実際の平均以上になった割合（p値）で見る。
- 前半期間でトリガーの深さとNを選び、後半期間でそのまま当てはめる。

【B. レトロスペクティブ（暴落が起きたと分かった上での振り返り）】
- 暴落局面＝指数が20日高値から−10%以下になった局面。各局面の底（その後15営業日内の最安値の日）の
  翌日の始値で個別銘柄を買った場合の20/60日後の上昇率を、
  「暴落前20日高値からの下落率」と属性（時価総額・PBR・配当利回り・暴落前の値動きの大きさ・
  暴落前60日の上昇率・業種）で分けて比べる。局面ごと・全局面合算の両方。
- 属性は暴落前（ピーク日以前）の値だけを使う。株価系は日次データ、財務系はピーク日以前の直近パネル日。
- 「局面平均との差」は同じ局面の全銘柄平均との差（地合いの戻りを除いた、属性ごとの戻りやすさ）。

【注意】
データ期間（約2年）に含まれる大きな暴落は数回しかない。銘柄数は多くても「暴落の回数」が少ないため、
統計的な結論は弱い（同じ暴落の中の銘柄は同じ方向に動く）。数字は傾向の参考として読むこと。

【出力】data/backtest_out/crashes_eval.json / .md
"""

import json
import os
import sys

import numpy as np
import pandas as pd

import repro
from universe_filters import filter_params

OUT_DIR = 'data/backtest_out'
SEED = 20260916
N_BOOT = 10000
ENTRY_DELAYS = (0, 1, 2, 3, 5, 10)
HOLDS = (5, 20, 60)
COOLDOWN = 20
TRIGGERS = [('1日で−3%以下', 'day', -0.03), ('1日で−4%以下', 'day', -0.04), ('1日で−5%以下', 'day', -0.05),
            ('20日高値から−7%以下', 'dd', -0.07), ('20日高値から−10%以下', 'dd', -0.10),
            ('20日高値から−15%以下', 'dd', -0.15)]


def build_index(daily):
    close = daily.pivot(index='date', columns='ticker', values='close').sort_index()
    opn = daily.pivot(index='date', columns='ticker', values='open').sort_index()
    trad = daily.pivot(index='date', columns='ticker', values='tradeable').sort_index()
    trad = trad.astype('boolean').fillna(False).astype(bool)
    mcap = daily.pivot(index='date', columns='ticker', values='mktcap_mil').sort_index().astype(float)
    ret = close / close.shift(1) - 1
    elig = trad.shift(1, fill_value=False) & ret.notna() & np.isfinite(ret)
    ew = ret.where(elig).mean(axis=1)
    w = mcap.shift(1).where(elig)
    cw = (ret.where(elig) * w).sum(axis=1) / w.sum(axis=1)
    idx = pd.DataFrame({'ew_ret': ew.fillna(0), 'cw_ret': cw.fillna(0)})
    idx['ew_level'] = (1 + idx['ew_ret']).cumprod()
    idx['cw_level'] = (1 + idx['cw_ret']).cumprod()
    idx['ew_dd20'] = idx['ew_level'] / idx['ew_level'].rolling(20, min_periods=1).max() - 1
    return idx, close, opn, trad, ret


def index_forward(close, opn, trad, i_entry, i_exit):
    """i_entry日の始値→i_exit日の終値。i_entry前日に取引可能だった銘柄の等金額平均。"""
    if i_entry < 1 or i_entry >= len(close.index) or i_exit >= len(close.index) or i_exit < i_entry:
        return None
    mask = trad.iloc[i_entry - 1]
    r = (close.iloc[i_exit][mask] / opn.iloc[i_entry][mask] - 1).replace([np.inf, -np.inf], np.nan).dropna()
    return float(r.mean()) if len(r) else None


def trigger_days(idx, kind, thr, start=25):
    series = (idx['ew_ret'] if kind == 'day' else idx['ew_dd20']).to_numpy()
    hits, last = [], -10 ** 9
    for k in range(start, len(series)):
        if series[k] <= thr and k - last >= COOLDOWN:
            hits.append(k)
            last = k
    return hits


def _stats(g, d_all, H):
    s_ = g[f'fwd_{H}'].dropna()
    ep_mean = g['episode'].map(d_all.groupby('episode')[f'fwd_{H}'].mean())
    ex = (g[f'fwd_{H}'] - ep_mean).dropna()
    return {f'{H}日後 平均': round(float(s_.mean()) * 100, 2) if len(s_) else None,
            f'{H}日後 勝率': round(float((s_ > 0).mean()) * 100, 1) if len(s_) else None,
            f'{H}日後 局面平均との差': round(float(ex.mean()) * 100, 2) if len(ex) else None}


def table(d, d_all, groupcol, bins=None, labels=None, min_n=15):
    if d.empty or groupcol not in d:
        return {}
    d = d.copy()
    d['g'] = pd.cut(d[groupcol].astype(float), bins, labels=labels) if bins is not None else d[groupcol]
    out = {}
    for gname, g in d.groupby('g', observed=True):
        if len(g) < min_n:
            continue
        row = {'銘柄数': int(len(g))}
        for H in (20, 60):
            row.update(_stats(g, d_all, H))
        out[str(gname)] = row
    return out


def all_tables(d, d_all):
    drop_bins = ([-1, -0.4, -0.3, -0.2, -0.1, 0.01], ['−40%以上', '−30〜−40%', '−20〜−30%', '−10〜−20%', '−10%未満'])
    return {
        'by_drop': table(d, d_all, 'drop_from_20d_high', *drop_bins),
        'by_size': table(d, d_all, 'mktcap_oku', [0, 300, 1000, 5000, 1e9], ['300億未満', '300〜1000億', '1000〜5000億', '5000億以上']),
        'by_pbr': table(d, d_all, 'pbr', [0, 0.7, 1.0, 1.5, 3, 1e9], ['0.7未満', '0.7〜1', '1〜1.5', '1.5〜3', '3以上']),
        'by_div_yield': table(d, d_all, 'div_yield_forecast', [-0.01, 0.01, 2, 3, 4, 100], ['無配', '〜2%', '2〜3%', '3〜4%', '4%〜']),
        'by_volatility': table(d, d_all, 'pre_vol', [0, 0.015, 0.025, 0.04, 1],
                               ['低（日次1.5%未満）', '中（1.5〜2.5%）', '高（2.5〜4%）', '非常に高（4%以上）']),
        'by_pre_momentum': table(d, d_all, 'pre_ret', [-1, -0.1, 0, 0.1, 0.3, 10], ['−10%以下', '−10〜0%', '0〜10%', '10〜30%', '30%超']),
        'by_sector': table(d, d_all, 'sector_name'),
        'by_size_x_drop': {
            size: table(d[(d['mktcap_oku'] >= lo) & (d['mktcap_oku'] < hi)], d_all, 'drop_from_20d_high',
                        [-1, -0.3, -0.2, -0.1, 0.01], ['−30%以上', '−20〜−30%', '−10〜−20%', '−10%未満'])
            for size, (lo, hi) in {'大型（1000億以上）': (1000, 1e12), '中小型（1000億未満）': (0, 1000)}.items()},
        'by_div_x_drop': {
            band: table(d[(d['div_yield_forecast'] >= lo) & (d['div_yield_forecast'] < hi)], d_all, 'drop_from_20d_high',
                        [-1, -0.3, -0.2, -0.1, 0.01], ['−30%以上', '−20〜−30%', '−10〜−20%', '−10%未満'])
            for band, (lo, hi) in {'配当利回り3%以上': (3, 100), '配当利回り3%未満（無配含む）': (-1, 3)}.items()},
    }


def main():
    daily = pd.read_pickle(os.path.join(OUT_DIR, 'daily.pkl'))
    panel = pd.read_pickle(os.path.join(OUT_DIR, 'panel.pkl'))
    idx, close, opn, trad, ret_all = build_index(daily)
    mcap = daily.pivot(index='date', columns='ticker', values='mktcap_mil').sort_index().astype(float)
    dates = list(idx.index)
    n = len(dates)
    split_k = n // 2
    rng = np.random.default_rng(SEED)

    # --- すべての日に買った場合（基準）。k = トリガー判定日 ---
    base = {}
    for N in ENTRY_DELAYS:
        for H in HOLDS:
            base[(N, H)] = {k: index_forward(close, opn, trad, k + 1 + N, k + N + H) for k in range(25, n)}

    def base_mean(N, H, lo=0, hi=None):
        vals = [v for k, v in base[(N, H)].items() if v is not None and k >= lo and (hi is None or k < hi)]
        return float(np.mean(vals)) if vals else None

    # --- A. プロスペクティブ ---
    prospective = []
    for label, kind, thr in TRIGGERS:
        ks = trigger_days(idx, kind, thr)
        for N in ENTRY_DELAYS:
            for H in HOLDS:
                ev = [(k, base[(N, H)].get(k)) for k in ks]
                ev = [(k, v) for k, v in ev if v is not None]
                if not ev:
                    continue
                vals = np.array([v for _, v in ev])
                pool = np.array([v for v in base[(N, H)].values() if v is not None])
                boot = rng.choice(pool, size=(N_BOOT, len(vals)), replace=True).mean(axis=1)
                first = [v for k, v in ev if k < split_k]
                second = [v for k, v in ev if k >= split_k]
                bf = base_mean(N, H, hi=split_k)
                prospective.append({
                    'trigger': label, 'entry_delay_days': N, 'hold_days': H, 'events': len(vals),
                    'event_dates': [dates[k] for k, _ in ev], 'mean_return_pct': round(vals.mean() * 100, 2),
                    'win_rate_pct': round(float((vals > 0).mean()) * 100, 1),
                    'all_days_mean_pct': round(base_mean(N, H) * 100, 2),
                    'p_value_random_days': round(float((boot >= vals.mean()).mean()), 4),
                    'first_half_events': len(first), 'second_half_events': len(second),
                    'first_half_mean_pct': round(float(np.mean(first)) * 100, 2) if first else None,
                    'second_half_mean_pct': round(float(np.mean(second)) * 100, 2) if second else None,
                    'first_half_edge_vs_all_days_pct': round((float(np.mean(first)) - bf) * 100, 2) if first and bf is not None else None,
                })

    chosen = {}
    for H in HOLDS:
        cands = [r for r in prospective if r['hold_days'] == H and r['first_half_events'] >= 1
                 and r['first_half_edge_vs_all_days_pct'] is not None]
        if not cands:
            continue
        best = max(cands, key=lambda r: (r['first_half_edge_vs_all_days_pct'], -r['entry_delay_days'], r['trigger']))
        sb = base_mean(best['entry_delay_days'], H, lo=split_k)
        chosen[str(H)] = {'rule': f"指数が{best['trigger']}の日の{best['entry_delay_days']}営業日後、翌日の始値で買い{H}日保有",
                          'first_half_events': best['first_half_events'],
                          'first_half_edge_pct': best['first_half_edge_vs_all_days_pct'],
                          'second_half_events': best['second_half_events'],
                          'second_half_mean_pct': best['second_half_mean_pct'],
                          'second_half_all_days_mean_pct': round(sb * 100, 2) if sb is not None else None}

    dd_levels = []
    for thr in (-0.05, -0.07, -0.10, -0.12, -0.15, -0.20):
        ks = trigger_days(idx, 'dd', thr)
        row = {'level': f'{int(round(thr * 100))}%', 'events': len(ks), 'dates': [dates[k] for k in ks]}
        for H in HOLDS:
            v = [base[(0, H)].get(k) for k in ks]
            v = [x for x in v if x is not None]
            row[f'{H}日'] = round(float(np.mean(v)) * 100, 2) if v else None
        row['すべての日_20日'] = round(base_mean(0, 20) * 100, 2)
        row['すべての日_60日'] = round(base_mean(0, 60) * 100, 2)
        dd_levels.append(row)

    # --- B. レトロスペクティブ ---
    ew_dd = idx['ew_dd20'].to_numpy()
    episodes = []
    for k in trigger_days(idx, 'dd', -0.10, start=20):
        trough_k = k + int(ew_dd[k:k + 16].argmin())
        lo = max(0, trough_k - 20)
        peak_k = lo + int(idx['ew_level'].iloc[lo:trough_k + 1].to_numpy().argmax())
        episodes.append((peak_k, trough_k))

    stock = []
    for peak_k, trough_k in episodes:
        entry_k = trough_k + 1
        if entry_k >= n:
            continue
        ep = f'{dates[peak_k]}→{dates[trough_k]}'
        pre_dates = panel.loc[panel['date'] <= dates[peak_k], 'date']
        attrs = panel[panel['date'] == pre_dates.max()].set_index('ticker') if len(pre_dates) else pd.DataFrame()
        for t in close.columns:
            if not trad[t].iat[trough_k]:
                continue
            pre_high = close[t].iloc[max(0, trough_k - 20):trough_k + 1].max()
            c_tr, e = close[t].iat[trough_k], opn[t].iat[entry_k]
            if not (pre_high > 0 and c_tr > 0 and e > 0):
                continue
            rec = {'episode': ep, 'ticker': t, 'drop_from_20d_high': c_tr / pre_high - 1}
            for H in (20, 60):
                j = trough_k + H
                v = close[t].iat[j] if j < n else np.nan
                rec[f'fwd_{H}'] = v / e - 1 if v == v else None
            r_pre = ret_all[t].iloc[max(1, peak_k - 59):peak_k + 1].dropna()
            rec['pre_vol'] = float(r_pre.std()) if len(r_pre) >= 10 else None
            c0 = close[t].iat[max(0, peak_k - 60)]
            rec['pre_ret'] = close[t].iat[peak_k] / c0 - 1 if c0 == c0 and c0 > 0 else None
            m = mcap[t].iat[peak_k]
            rec['mktcap_oku'] = m / 100 if m == m else None
            if len(attrs) and t in attrs.index:
                a = attrs.loc[t]
                for col in ('pbr', 'div_yield_forecast', 'sector_name', 'equity_ratio'):
                    rec[col] = a.get(col)
            stock.append(rec)
    sdf = pd.DataFrame(stock)
    retro = {'episodes': [{'peak_date': dates[p], 'trough_date': dates[t], 'index_drawdown_pct': round(float(ew_dd[t]) * 100, 2),
                           'stocks': int((sdf['episode'] == f'{dates[p]}→{dates[t]}').sum()) if len(sdf) else 0}
                          for p, t in episodes]}
    if len(sdf):
        retro['pooled'] = all_tables(sdf, sdf)
        retro['per_episode'] = {ep: all_tables(g, sdf) for ep, g in sdf.groupby('episode')}

    report = {'meta': repro.run_metadata(sorted(daily['ticker'].unique()),
                                         {'seed': SEED, 'n_boot': N_BOOT, 'entry_delays': ENTRY_DELAYS, 'holds': HOLDS,
                                          'cooldown': COOLDOWN, 'triggers': TRIGGERS, 'universe_filter': filter_params()},
                                         ['evaluate_crashes.py', 'build_panel.py', 'universe_filters.py']),
              'period': [dates[0], dates[-1]], 'split_date': dates[split_k], 'prospective': prospective,
              'prospective_chosen_by_first_half': chosen, 'index_drawdown_levels': dd_levels, 'retrospective': retro}
    with open(os.path.join(OUT_DIR, 'crashes_eval.json'), 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    with open(os.path.join(OUT_DIR, 'crashes_eval.md'), 'w', encoding='utf-8') as f:
        f.write(render_markdown(report))
    print('[crashes] 完了', flush=True)


def render_markdown(r):
    lines = ['# 暴落後に買う戦略の検証', '',
             f"- 期間: {r['period'][0]}〜{r['period'][1]} / 後半の開始日: {r['split_date']}",
             '- 指数: 前日に取引可能だった銘柄の等金額平均（売買代金不足・仕手株化を除外、上場廃止を含む）',
             f"- p値: 同じ数の日付をランダムに選ぶ試行{N_BOOT:,}回で、実際の平均以上になった割合（乱数の種 {SEED}）。0.05未満なら偶然とは考えにくい目安",
             '- 注意: 期間中の暴落は数回しかなく、統計的な結論は弱い。', '',
             '## A. プロスペクティブ（その時点で分かるルール）', '',
             '| トリガー | 何営業日後に買う | 保有 | 回数 | 平均 | 勝率 | すべての日の平均 | p値 | 発動日 |',
             '|---|---:|---:|---:|---:|---:|---:|---:|---|']
    for x in r['prospective']:
        if x['hold_days'] not in (20, 60) or x['entry_delay_days'] not in (0, 1, 3, 5, 10):
            continue
        lines.append(f"| {x['trigger']} | {x['entry_delay_days']} | {x['hold_days']} | {x['events']} | {x['mean_return_pct']}% | "
                     f"{x['win_rate_pct']}% | {x['all_days_mean_pct']}% | {x['p_value_random_days']} | {', '.join(x['event_dates'])} |")
    lines += ['', '前半期間で選んだルールを後半に当てはめた結果:', '']
    for H, c in r['prospective_chosen_by_first_half'].items():
        lines.append(f"- {c['rule']}: 前半{c['first_half_events']}回で上乗せ {c['first_half_edge_pct']}% → "
                     f"後半 {c['second_half_events']}回 平均 {c['second_half_mean_pct']}%（後半のすべての日の平均 {c['second_half_all_days_mean_pct']}%）")
    lines += ['', '### 指数が20日高値から何%下がった日に買うか（翌日の始値で買い）', '',
              '| 下落率 | 回数 | 5日後 | 20日後 | 60日後 | すべての日（20日/60日） | 日付 |', '|---|---:|---:|---:|---:|---|---|']
    for x in r['index_drawdown_levels']:
        lines.append(f"| {x['level']} | {x['events']} | {x['5日']}% | {x['20日']}% | {x['60日']}% | "
                     f"{x['すべての日_20日']}% / {x['すべての日_60日']}% | {', '.join(x['dates'])} |")

    rt = r['retrospective']
    lines += ['', '## B. レトロスペクティブ（暴落が起きたと分かった上での振り返り）', '',
              '暴落局面（指数が20日高値から−10%以下）: ' + (' / '.join(
                  f"{e['peak_date']}→底{e['trough_date']}（{e['index_drawdown_pct']}%、{e['stocks']}銘柄）" for e in rt['episodes']) or 'なし'),
              '', '各局面の底の翌日の始値で買った場合。「局面平均との差」は同じ局面の全銘柄平均との差。', '']

    def emit(tabs, heading):
        out = [f'### {heading}', '']
        for key, title in (('by_drop', '暴落前20日高値からの下落率'), ('by_size', '時価総額'), ('by_pbr', 'PBR'),
                           ('by_div_yield', '予想配当利回り'), ('by_volatility', '暴落前の値動きの大きさ（日次）'),
                           ('by_pre_momentum', '暴落前60日の上昇率'), ('by_sector', '業種')):
            tab = tabs.get(key) or {}
            out += [f'#### {title}', '',
                    '| 区分 | 銘柄数 | 20日後 平均 | 20日後 局面平均との差 | 60日後 平均 | 60日後 勝率 | 60日後 局面平均との差 |',
                    '|---|---:|---:|---:|---:|---:|---:|']
            for g, v in tab.items():
                out.append(f"| {g} | {v['銘柄数']} | {v['20日後 平均']}% | {v['20日後 局面平均との差']}% | {v['60日後 平均']}% | "
                           f"{v['60日後 勝率']}% | {v['60日後 局面平均との差']}% |")
            out.append('')
        for key, title in (('by_size_x_drop', '時価総額×下落率'), ('by_div_x_drop', '配当利回り×下落率')):
            out += [f'#### {title}', '']
            for grp, tab in (tabs.get(key) or {}).items():
                out.append(f"- {grp}: " + (' / '.join(
                    f"{g} 60日後{v['60日後 平均']}%（勝率{v['60日後 勝率']}%、局面平均との差{v['60日後 局面平均との差']}%、{v['銘柄数']}銘柄）"
                    for g, v in tab.items()) or '該当が少ない'))
            out.append('')
        return out

    if rt.get('pooled'):
        lines += emit(rt['pooled'], '全局面の合算')
        for ep, tabs in rt.get('per_episode', {}).items():
            lines += emit(tabs, f'局面 {ep}')
    return '\n'.join(lines)


if __name__ == '__main__':
    sys.exit(main())
