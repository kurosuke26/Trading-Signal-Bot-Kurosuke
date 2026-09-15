#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_panel.py — 検証用の「特徴量パネル」を作る（2026-09-16追加）。

全銘柄（現在の上場銘柄＋期間中の上場廃止銘柄）について、PANEL_STEP営業日ごとに
  - その日の引けまでに分かる株価系の指標（リターン・52週高値・移動平均・出来高・ATRなど）
  - その日までに開示されたファンダメンタルズ（fundamental_features.py）
  - 取引可能フラグ（universe_filters.py：売買代金不足・仕手株化の除外）
  - その後のリターン（翌営業日始値→5/20/60営業日後の終値。上場廃止なら最終取引日の終値）
を1行にまとめ、data/backtest_out/panel.pkl に保存する。あわせて配当予想の変化（増配・減配の発表）の
一覧を data/backtest_out/dividend_events.pkl に、全銘柄の日次リターンを daily_returns.pkl に保存する。

「その後のリターン」は成績を測るためだけの列で、特徴量（スコア・条件）の計算には一切使わない
（列名は fwd_ で始まる）。

【実行方法】
  python build_panel.py            # 全銘柄（10分前後）
  python build_panel.py --max-tickers 50
"""

import argparse
import json
import os
import sys
from bisect import bisect_right
from multiprocessing import Pool

import numpy as np
import pandas as pd

import repro
from evaluate_sakata import load_adjusted_bars, load_eval_universe
from fast_features import precompute_technicals
from fundamental_features import FundamentalState, load_fins_records
from universe_filters import filter_params, tradeable_flags

CACHE_DIR = os.getenv('JQUANTS_CACHE_DIR') or 'data/jquants_cache'
OUT_DIR = 'data/backtest_out'
PANEL_STEP = 5
HORIZONS = (5, 20, 60)
MIN_ROWS = 20  # 2024-08-05の暴落より前からパネルを作るため短め。長い期間が必要な指標はその間 NaN
CALENDAR_TICKER = '7203'  # 営業日カレンダーの基準（全営業日の行がある大型株）


def _sector_map():
    with open(os.path.join(CACHE_DIR, '_universe.json'), encoding='utf-8') as f:
        return {u['ticker4']: (u.get('sector_code'), u.get('sector_name')) for u in json.load(f)['universe']}


def panel_dates():
    with open(os.path.join(CACHE_DIR, CALENDAR_TICKER, 'bars.json'), encoding='utf-8') as f:
        dates = sorted({r['Date'] for r in json.load(f)})
    return dates[MIN_ROWS::PANEL_STEP], dates


def build_ticker(args):
    (ticker4, is_delisted), panel_date_set, sector = args
    loaded = load_adjusted_bars(ticker4, return_raw=True)
    if loaded is None:
        return None
    df, raw = loaded
    n = len(df)
    if n < MIN_ROWS + 2:
        return None
    dates = [d.strftime('%Y-%m-%d') for d in df.index]
    O = df['Open'].to_numpy(float)
    H = df['High'].to_numpy(float)
    L = df['Low'].to_numpy(float)
    C = df['Close'].to_numpy(float)
    V = df['Value'].to_numpy(float)
    raw_c = df['RawClose'].to_numpy(float)
    cum = np.cumprod(df['AdjFactor'].to_numpy(float))
    raw_sorted = raw.assign(Date=raw['Date'].astype(str)).sort_values('Date')
    mcap_by_date = dict(zip(raw_sorted['Date'], pd.to_numeric(raw_sorted.get('MktCap'), errors='coerce')))

    def cum_at(date_str):
        j = bisect_right(dates, date_str) - 1
        return cum[j] if j >= 0 else 1.0

    pre = precompute_technicals(df)
    tradeable, reasons = tradeable_flags(raw, df, pre['atr'])
    close_s = pd.Series(C)
    ma50 = close_s.rolling(50).mean().to_numpy()
    ma150 = close_s.rolling(150).mean().to_numpy()
    ma200 = close_s.rolling(200).mean().to_numpy()
    hi250 = pd.Series(H).rolling(250, min_periods=250).max().to_numpy()
    lo250 = pd.Series(L).rolling(250, min_periods=250).min().to_numpy()
    hi55_prev = pd.Series(H).shift(1).rolling(55).max().to_numpy()
    hi20_prev = pd.Series(H).shift(1).rolling(20).max().to_numpy()
    hi250_prev = pd.Series(H).shift(1).rolling(250, min_periods=250).max().to_numpy()
    maxc60 = close_s.rolling(60).max().to_numpy()
    val20 = pd.Series(V).rolling(20).mean().to_numpy()
    val60_prev = pd.Series(V).shift(20).rolling(60).mean().to_numpy()

    records = load_fins_records(ticker4)
    state = FundamentalState(cum_at)
    rp = 0
    rows = []
    for i in range(MIN_ROWS, n):
        d = dates[i]
        while rp < len(records) and records[rp]['DiscDate'] <= d:
            state.apply(records[rp])
            rp += 1
        if d not in panel_date_set:
            continue
        mcap = mcap_by_date.get(d)
        mcap = float(mcap) if mcap is not None and mcap == mcap else None
        row = {'ticker': ticker4, 'date': d, 'delisted': is_delisted, 'sector_code': sector[0],
               'sector_name': sector[1], 'tradeable': bool(tradeable[i]), 'exclude_reason': reasons[i] or None,
               'price': raw_c[i] if raw_c[i] == raw_c[i] else None}
        row.update(state.features(d, cum[i], row['price'], mcap))

        def ret(k):
            return C[i] / C[i - k] - 1 if i - k >= 0 and C[i - k] > 0 else None

        row['ret_5'], row['ret_20'], row['ret_60'], row['ret_120'] = ret(5), ret(20), ret(60), ret(120)
        row['mom_12_1'] = C[i - 20] / C[i - 250] - 1 if i >= 250 and C[i - 250] > 0 else None
        row['high_52w_ratio'] = C[i] / hi250[i] if hi250[i] == hi250[i] else None
        row['low_52w_ratio'] = C[i] / lo250[i] if lo250[i] == lo250[i] and lo250[i] > 0 else None
        row['new_high_52w'] = bool(hi250_prev[i] == hi250_prev[i] and C[i] > hi250_prev[i]) if i >= 250 else None
        row['breakout_55'] = bool(C[i] > hi55_prev[i]) if hi55_prev[i] == hi55_prev[i] else None
        row['breakout_20'] = bool(C[i] > hi20_prev[i]) if hi20_prev[i] == hi20_prev[i] else None
        row['above_ma50'] = bool(C[i] > ma50[i]) if ma50[i] == ma50[i] else None
        row['trend_template'] = None
        if i >= 220 and ma200[i - 20] == ma200[i - 20]:
            row['trend_template'] = bool(C[i] > ma150[i] > ma200[i] and ma200[i] > ma200[i - 20] and C[i] > ma50[i])
        row['value_ratio_20_60'] = val20[i] / val60_prev[i] if val60_prev[i] and val60_prev[i] > 0 else None
        row['avg_value_20_oku'] = val20[i] / 1e8 if val20[i] == val20[i] else None
        row['atr_pct'] = pre['atr'][i] / C[i] if pre['atr'][i] == pre['atr'][i] else None
        row['drawdown_60'] = C[i] / maxc60[i] - 1 if maxc60[i] == maxc60[i] else None
        row['dist_ma25'] = C[i] / pre['ma25'][i] - 1 if pre['ma25'][i] == pre['ma25'][i] else None
        row['rsi14'] = pre['rsi'][i] if pre['rsi'][i] == pre['rsi'][i] else None

        entry = O[i + 1] if i + 1 < n else None
        for h in HORIZONS:
            key = f'fwd_{h}'
            if not entry or entry <= 0:
                row[key] = None
            elif i + h < n:
                row[key] = C[i + h] / entry - 1
            elif is_delisted:
                row[key] = C[n - 1] / entry - 1  # 上場廃止：最終取引日の終値で手仕舞い
            else:
                row[key] = None  # データ期間の外
        rows.append(row)

    # 配当予想の変化（増配・減配の発表）を t日の株数ベースの値に直して返す
    events = []
    for ev in state.dividend_events:
        j = bisect_right(dates, ev['disc_date']) - 1
        c_now = cum[j] if j >= 0 else 1.0
        e = {'ticker': ticker4, 'disc_date': ev['disc_date'], 'fy_end': ev['fy_end'], 'source': ev['source'],
             'delisted': is_delisted, 'sector_name': sector[1]}
        for k in ('new_div_norm', 'prev_forecast_norm', 'prev_actual_norm'):
            e[k.replace('_norm', '')] = ev[k] * c_now if ev[k] is not None else None
        events.append(e)

    daily = pd.DataFrame({'ticker': ticker4, 'date': dates, 'open': O, 'close': C, 'value': V,
                          'mktcap_mil': [mcap_by_date.get(d) for d in dates], 'tradeable': tradeable})
    return rows, events, daily


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--max-tickers', type=int, default=0)
    ap.add_argument('--workers', type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument('--suffix', default='')
    args = ap.parse_args()

    universe = sorted(load_eval_universe(args.max_tickers))
    pdates, _ = panel_dates()
    pset = set(pdates)
    sectors = _sector_map()
    jobs = [(u, pset, sectors.get(u[0], (None, None))) for u in universe]
    print(f'[panel] {len(universe)}銘柄（うち上場廃止 {sum(1 for _, d in universe if d)}）/ パネル日 {len(pdates)}日 '
          f'（{pdates[0]}〜{pdates[-1]}、{PANEL_STEP}営業日ごと）', flush=True)

    all_rows, all_events, dailies = [], [], []
    with Pool(args.workers) as pool:
        for k, res in enumerate(pool.imap(build_ticker, jobs, chunksize=4)):
            if res:
                all_rows.extend(res[0])
                all_events.extend(res[1])
                dailies.append(res[2])
            if (k + 1) % 500 == 0:
                print(f'[panel] {k + 1}/{len(jobs)}', flush=True)

    panel = pd.DataFrame(all_rows).sort_values(['date', 'ticker']).reset_index(drop=True)
    events = pd.DataFrame(all_events).sort_values(['disc_date', 'ticker']).reset_index(drop=True)
    daily = pd.concat(dailies).sort_values(['date', 'ticker']).reset_index(drop=True)
    os.makedirs(OUT_DIR, exist_ok=True)
    sfx = args.suffix
    panel.to_pickle(os.path.join(OUT_DIR, f'panel{sfx}.pkl'))
    events.to_pickle(os.path.join(OUT_DIR, f'dividend_events{sfx}.pkl'))
    daily.to_pickle(os.path.join(OUT_DIR, f'daily{sfx}.pkl'))
    meta = repro.run_metadata([t for t, _ in universe],
                              {'panel_step': PANEL_STEP, 'horizons': HORIZONS, 'min_rows': MIN_ROWS,
                               'calendar_ticker': CALENDAR_TICKER, 'universe_filter': filter_params()},
                              ['build_panel.py', 'fundamental_features.py', 'fast_features.py', 'universe_filters.py',
                               'evaluate_sakata.py'])
    with open(os.path.join(OUT_DIR, f'panel{sfx}_meta.json'), 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f'[panel] 完了: 行 {len(panel):,} / 配当予想イベント {len(events):,} / 日次 {len(daily):,}', flush=True)


if __name__ == '__main__':
    sys.exit(main())
