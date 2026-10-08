#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
extend_bars_yfinance.py — J-Quants無料プラン（12週間遅れ）で取れない直近の日足を、本番の日次収集（collector.py）と
同じYahoo! Financeから取ってつなぎ、「延長版キャッシュ」を別フォルダに作る（2026-10-08追加。検証専用）。

【作るもの】
OUT_CACHE_DIR/<銘柄>/bars.json  … J-Quantsのbars.jsonの後ろに、J-Quantsの最終日より後の日足を同じ形式で追加
OUT_CACHE_DIR/<銘柄>/fins.json  … 元のキャッシュのまま（決算はJ-Quantsにある分だけ。本番の判定と同じ条件）
OUT_CACHE_DIR/_universe.json・_delisted.json … 元のキャッシュのまま
元の data/jquants_cache は一切変更しない。build_panel.py などは JQUANTS_CACHE_DIR=OUT_CACHE_DIR で読む。

【つなぎ方と確認】
- Yahoo!の日足は auto_adjust=False（分割は反映・配当は未調整＝J-Quantsの生の終値Cに相当）を使う。
- J-Quantsと重なる期間（直近OVERLAP_DAYS営業日）の終値を比べ、平均のずれがMAX_OVERLAP_DIFF以内の銘柄だけつなぐ
  （ずれが大きい＝分割・配信データの違い等。つながずにJ-Quantsの最終日で止める＝その後の判定には出てこない）。
- 追加分は分割が無かったものとして AdjFactor=1、調整後の値＝生の値。売買代金＝終値×出来高。
- 時価総額はJ-Quantsの最終日の値に株価の変化率を掛けた近似（株数が変わらない前提）。
- ストップ高・安のフラグは取れないため'0'（仕手株の除外条件の1つが働かない。影響は小さい）。

使い方: python extend_bars_yfinance.py <OUT_CACHE_DIR> [--end 2026-10-07]
"""

import argparse
import json
import os
import shutil
import sys
import time

import numpy as np
import pandas as pd
import yfinance as yf

SRC_DIR = os.getenv('JQUANTS_CACHE_DIR') or 'data/jquants_cache'
OVERLAP_DAYS = 10
MAX_OVERLAP_DIFF = 0.01
CHUNK = 100


def load_bars(t4):
    path = os.path.join(SRC_DIR, t4, 'bars.json')
    if not os.path.exists(path):
        return None
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('out_dir')
    ap.add_argument('--end', default=None, help='取得する最終日の翌日（yfinanceのend、既定は今日）')
    args = ap.parse_args()
    out = args.out_dir
    os.makedirs(out, exist_ok=True)
    for name in ('_universe.json', '_delisted.json'):
        if os.path.exists(os.path.join(SRC_DIR, name)):
            shutil.copy2(os.path.join(SRC_DIR, name), os.path.join(out, name))

    tickers = sorted(d for d in os.listdir(SRC_DIR) if os.path.isdir(os.path.join(SRC_DIR, d)) and not d.startswith('_'))
    bars = {t: load_bars(t) for t in tickers}
    last_dates = [b[-1]['Date'] for b in bars.values() if b]
    jq_last = max(last_dates)
    start = (pd.Timestamp(jq_last) - pd.Timedelta(days=30)).strftime('%Y-%m-%d')
    print(f'[extend] {len(tickers)}銘柄／J-Quantsの最終日 {jq_last}／Yahoo!から {start} 以降を取得', flush=True)

    # 最終日がJ-Quantsの最終日から10日以内（＝今も上場中）の銘柄だけ延長する。それ以外（上場廃止など）はそのまま写す
    live_from = (pd.Timestamp(jq_last) - pd.Timedelta(days=10)).strftime('%Y-%m-%d')
    live = [t for t in tickers if bars[t] and bars[t][-1]['Date'] >= live_from]
    yfd = {}
    for i in range(0, len(live), CHUNK):
        chunk = live[i:i + CHUNK]
        syms = [f'{t}.T' for t in chunk]
        for attempt in range(3):
            try:
                data = yf.download(syms, start=start, end=args.end, interval='1d', auto_adjust=False,
                                   group_by='ticker', threads=True, progress=False)
                break
            except Exception as e:  # noqa: BLE001
                print(f'[extend] 取得失敗（{attempt + 1}回目）: {e}', flush=True)
                time.sleep(5 * (attempt + 1))
        else:
            continue
        for t, s in zip(chunk, syms):
            try:
                df = data[s].dropna(subset=['Open', 'High', 'Low', 'Close'])
            except (KeyError, TypeError):
                continue
            if len(df):
                yfd[t] = df
        print(f'[extend] {min(i + CHUNK, len(live))}/{len(live)}（取得できた銘柄 {len(yfd)}）', flush=True)
        time.sleep(1.5)

    stats = {'extended': 0, 'mismatch': 0, 'no_yahoo': 0, 'copied_as_is': 0}
    mismatches = []
    for t in tickers:
        b = bars[t]
        os.makedirs(os.path.join(out, t), exist_ok=True)
        src_fins = os.path.join(SRC_DIR, t, 'fins.json')
        if os.path.exists(src_fins):
            shutil.copy2(src_fins, os.path.join(out, t, 'fins.json'))
        new_rows = []
        if t in yfd and b:
            df = yfd[t]
            jq_close = {r['Date']: r.get('C') for r in b[-OVERLAP_DAYS:] if r.get('C')}
            yidx = {d.strftime('%Y-%m-%d'): float(c) for d, c in zip(df.index, df['Close'])}
            common = [d for d in jq_close if d in yidx]
            diff = np.mean([abs(yidx[d] / jq_close[d] - 1) for d in common]) if common else 1.0
            if len(common) >= 3 and diff <= MAX_OVERLAP_DIFF:
                last = b[-1]
                mcap0, c0 = last.get('MktCap'), last.get('C')
                for d, row in df.iterrows():
                    ds = d.strftime('%Y-%m-%d')
                    if ds <= last['Date']:  # その銘柄のJ-Quantsの最終日より後だけを足す
                        continue
                    o, h, lo, c, v = (float(row[k]) for k in ('Open', 'High', 'Low', 'Close', 'Volume'))
                    new_rows.append({'Date': ds, 'Code': last.get('Code'), 'O': o, 'H': h, 'L': lo, 'C': c,
                                     'UL': '0', 'LL': '0', 'Vo': v, 'Va': c * v, 'AdjFactor': 1.0,
                                     'AdjO': o, 'AdjH': h, 'AdjL': lo, 'AdjC': c, 'AdjVo': v,
                                     'MktCap': (mcap0 * c / c0) if mcap0 and c0 else None, 'ExRT': None,
                                     'source': 'yahoo'})
                stats['extended'] += 1
            else:
                stats['mismatch'] += 1
                mismatches.append((t, round(float(diff), 4), len(common)))
        elif t in live:
            stats['no_yahoo'] += 1
        else:
            stats['copied_as_is'] += 1
        with open(os.path.join(out, t, 'bars.json'), 'w', encoding='utf-8') as f:
            json.dump((b or []) + new_rows, f, ensure_ascii=False)
    with open(os.path.join(out, '_extend_report.json'), 'w', encoding='utf-8') as f:
        json.dump({'jq_last': jq_last, 'stats': stats, 'mismatches': mismatches}, f, ensure_ascii=False, indent=1)
    print(f'[extend] 完了: {stats}', flush=True)


if __name__ == '__main__':
    sys.exit(main())
