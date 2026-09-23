#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
repair_split_adjustments.py — 既に記録済みのポジションのうち、エントリー後に株式分割があったものを直す（2026-09-23追加）。

株価は分割・配当調整済み（auto_adjust=True）で取得しているため、分割が起きると過去の株価が遡って調整される。
一方、記録済みの取得価格は分割前の水準のままなので、放置すると損益が実態とかけ離れる
（例：9279.T は 2026-08-28 に1:2分割。取得5,040円に対し最新2,016円で「60%の下落」に見えていた）。

これ以降に発生する分割は tracking.apply_split_adjustment / event_strategies._apply_split_adjustment が
毎晩の更新時に自動で揃えるため、このスクリプトは過去分の手当て（一回限り）。

使い方: python repair_split_adjustments.py [--dry-run]
"""

import sys
import warnings

warnings.filterwarnings('ignore')
import pandas as pd
import yfinance as yf

import event_strategies
import tracking
from util import price_adjust_factor

CHUNK = 200
RETURN_DIFF_TOLERANCE = 1.0  # 決済済みの損益率がこれ以上ずれていたら、分割後の水準で計算し直す


def fetch_histories(tickers, period='3mo'):
    out = {}
    tickers = sorted(set(tickers))
    for k in range(0, len(tickers), CHUNK):
        chunk = tickers[k:k + CHUNK]
        data = yf.download(chunk, period=period, auto_adjust=True, group_by='ticker', threads=True, progress=False)
        for t in chunk:
            try:
                df = data[t] if isinstance(data.columns, pd.MultiIndex) else data
                df = df.dropna(subset=['Close'])
                if len(df):
                    out[t] = df
            except (KeyError, ValueError):
                continue
        print(f'[repair] {min(k + CHUNK, len(tickers))}/{len(tickers)}', flush=True)
    return out


def repair_trade_log(histories, today_str, dry_run):
    log = tracking.load_trade_log()
    fixed = []
    for p in log:
        before = p.get('entry_price')
        f = tracking.apply_split_adjustment(p, histories.get(p['ticker']), today_str)
        if not f:
            continue
        kind = '分割' if f < 0.9 or f > 1.1 else '配当調整など'
        note = [f"{p['ticker']} {p.get('name')} {p['signal']} {p['entry_date']} {kind} 倍率x{f:.4g} "
                f"取得{before}→{p['entry_price']}"]
        # 決済済みのバリエーションは、取得日と決済日の両方を今の株価系列から取り直して計算し直す
        # （記録された決済価格は調整前の水準であることがあり、調整後の取得価格と混ぜられないため）
        df = histories.get(p['ticker'])
        for key, v in (p.get('variants') or {}).items():
            if v.get('status') != 'closed' or v.get('close_date') is None or v.get('return_pct') is None:
                continue
            row = df[df.index.strftime('%Y-%m-%d') == v['close_date']] if df is not None else None
            if row is None or not len(row):
                note.append(f"  変倍率{key}: 決済日{v['close_date']}の株価が取れず、損益はそのまま")
                continue
            e, c = p['entry_price'], round(float(row['Close'].iloc[-1]), 2)
            new = (c - e) / e * 100 if p['signal'] == 'LONG' else (e - c) / e * 100
            if abs(new - v['return_pct']) > RETURN_DIFF_TOLERANCE:
                note.append(f"  変倍率{key}: 損益 {v['return_pct']:+.2f}% → {new:+.2f}%"
                            f"（決済{v['close_date']} 価格{v['close_price']}→{c}）")
                v['close_price'], v['return_pct'] = c, round(new, 2)
        fixed.append('\n'.join(note))
    if fixed and not dry_run:
        tracking.save_trade_log(log)
    return fixed


def repair_event_log(histories, dry_run):
    ev = event_strategies._load_json(event_strategies.EVENT_TRADE_LOG_PATH, [])
    fixed = []
    for p in ev:
        if not p.get('entry_price') or p.get('entry_date') is None:
            continue
        df = histories.get(p['ticker'])
        if df is None:
            continue
        same = df[df.index.strftime('%Y-%m-%d') == p['entry_date']]
        f = price_adjust_factor(p['entry_price'], float(same['Open'].iloc[-1]) if len(same) else None)
        if not f:
            continue
        before = p['entry_price']
        for k in ('entry_price', 'stop', 'take_profit', 'last_price', 'exit_price'):
            if p.get(k):
                p[k] = round(p[k] * f, 2)
        p.setdefault('adjustments', []).append(round(f, 6))
        fixed.append(f"{p['ticker']} {p.get('series')} {p['entry_date']} 倍率x{f:.4g} 取得{before}→{p['entry_price']}")
    if fixed and not dry_run:
        event_strategies._save_json(event_strategies.EVENT_TRADE_LOG_PATH, ev)
    return fixed


def main():
    dry_run = '--dry-run' in sys.argv
    log = tracking.load_trade_log()
    ev = event_strategies._load_json(event_strategies.EVENT_TRADE_LOG_PATH, [])
    tickers = [p['ticker'] for p in log if p.get('entry_price')] + [p['ticker'] for p in ev if p.get('entry_price')]
    histories = fetch_histories(tickers)
    today_str = pd.Timestamp.utcnow().strftime('%Y-%m-%d')
    a = repair_trade_log(histories, today_str, dry_run)
    b = repair_event_log(histories, dry_run)
    print('\n■ 本番の記録（data/trade_log.json）')
    print('\n'.join(a) if a else '  分割の影響を受けた記録はありません')
    print('\n■ 別枠の記録（data/event_trade_log.json）')
    print('\n'.join(b) if b else '  分割の影響を受けた記録はありません')
    print(f"\n{'（--dry-run のため保存していません）' if dry_run else '保存しました'}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
