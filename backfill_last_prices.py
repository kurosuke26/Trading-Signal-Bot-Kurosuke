#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backfill_last_prices.py — 保有中の仮想ポジションに、Yahoo Finance の最新の終値を書き込む（2026-09-22追加）。

含み損益の表示（tracking.compute_valuation / event_strategies.performance）は、各ポジションの
p['last_price']（最新の終値）を使う。この値は collector.py の毎晩の実行で更新されるが、表示を追加した直後は
まだ記録が無いため、この一回限りのスクリプトで全保有ポジションの最新終値を埋める。
（運用開始時の2026-08-25の一括分にも価格を入れる。表示の集計からは従来どおり除外し、参考値として別に出す）

使い方: python backfill_last_prices.py
"""

import json
import sys

import pandas as pd
import yfinance as yf

import event_strategies
import tracking

CHUNK = 200


def latest_closes(tickers):
    out = {}
    tickers = sorted(set(tickers))
    for k in range(0, len(tickers), CHUNK):
        chunk = tickers[k:k + CHUNK]
        data = yf.download(chunk, period='10d', auto_adjust=True, group_by='ticker', threads=True, progress=False)
        for t in chunk:
            try:
                df = data[t] if isinstance(data.columns, pd.MultiIndex) else data
                s = df['Close'].dropna()
                if len(s):
                    out[t] = (float(s.iloc[-1]), s.index[-1].strftime('%Y-%m-%d'))
            except (KeyError, ValueError):
                continue
        print(f'[backfill_prices] {min(k + CHUNK, len(tickers))}/{len(tickers)}', flush=True)
    return out


def main():
    log = tracking.load_trade_log()
    ev = event_strategies._load_json(event_strategies.EVENT_TRADE_LOG_PATH, [])
    need = [p['ticker'] for p in log if p.get('status') == 'open'] + \
           [p['ticker'] for p in ev if p.get('state') in ('open', 'exit_next_open')]
    prices = latest_closes(need)
    n1 = n2 = 0
    for p in log:
        if p.get('status') == 'open' and p['ticker'] in prices:
            p['last_price'], p['last_price_date'] = round(prices[p['ticker']][0], 2), prices[p['ticker']][1]
            n1 += 1
    for p in ev:
        if p.get('state') in ('open', 'exit_next_open') and p['ticker'] in prices:
            p['last_price'], p['last_price_date'] = round(prices[p['ticker']][0], 2), prices[p['ticker']][1]
            n2 += 1
    tracking.save_trade_log(log)
    event_strategies._save_json(event_strategies.EVENT_TRADE_LOG_PATH, ev)
    missing = sorted({t for t in need if t not in prices})
    print(f'[backfill_prices] 本番の記録 {n1}件・別枠の記録 {n2}件に最新終値を書き込みました。取得できなかった銘柄 {len(missing)}件: {missing[:20]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
