#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fast_features.py — バックテスト・検証スクリプト用に、本番の指標計算（indicators.py）を
「銘柄ごとに全期間を1回で計算し、各日の値を取り出す」形で高速に再現するモジュール。

本番の compute_technical_snapshot() / detect_volume_surge_quiet_price() は「その日までの直近220本」を
毎日渡して計算するため、全銘柄×全日（約140万回）では数時間かかる。ここでは同じ式を全期間の系列に
一度だけ適用し、i日目の値を取り出す。

【先読みが起きない理由】
移動平均・EWM（RSI/MACD/ATR）・ローリング平均は、いずれも i日目の値が i日目以前のデータだけで決まる。
【本番との差】
EWMは計算の起点（全期間の先頭か、220本窓の先頭か）でごくわずかに値が変わるが、220本前の影響は
(13/14)^219 ≒ 1e-7 程度で実質ゼロ。verify_against_production() で実データの一致を確認できる。
"""

import numpy as np
import pandas as pd

from indicators import rsi_zone


def _cross_flags(diff):
    """indicators.compute_moving_averages / compute_macd の「直近3営業日以内のクロス」判定を全日分まとめて行う。"""
    n = len(diff)
    up = np.zeros(n, dtype=bool)
    down = np.zeros(n, dtype=bool)
    sign = np.sign(diff)
    for i in range(n):
        if i < 3 or np.isnan(diff[i - 3:i + 1]).any():
            continue
        s = sign[i - 3:i + 1]
        changes = np.flatnonzero(s[:-1] != s[1:])
        if changes.size:
            if diff[i - 3 + changes[-1] + 1] > 0:
                up[i] = True
            else:
                down[i] = True
    return up, down


def precompute_technicals(df):
    close = df['Close'].astype(float)
    high, low = df['High'].astype(float), df['Low'].astype(float)
    out = {}
    out['close'] = close.to_numpy()
    sma5 = close.rolling(5).mean()
    sma25 = close.rolling(25).mean()
    sma75 = close.rolling(75).mean()
    out['ma5'], out['ma25'], out['ma75'] = sma5.to_numpy(), sma25.to_numpy(), sma75.to_numpy()
    out['gc'], out['dc'] = _cross_flags((sma5 - sma25).to_numpy())

    delta = close.diff()
    gain, loss = delta.clip(lower=0), -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    rsi = 100 - (100 / (1 + avg_gain / avg_loss.replace(0, np.nan)))
    out['rsi'] = rsi.where(avg_loss != 0, 100.0).to_numpy()

    ema_fast = close.ewm(span=12, adjust=False).mean()
    ema_slow = close.ewm(span=26, adjust=False).mean()
    macd = ema_fast - ema_slow
    signal = macd.ewm(span=9, adjust=False).mean()
    out['macd'], out['signal'] = macd.to_numpy(), signal.to_numpy()
    out['hist'] = (macd - signal).to_numpy()
    out['mbc'], out['mdc'] = _cross_flags((macd - signal).to_numpy())

    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    out['atr'] = tr.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean().to_numpy()
    return out


def _f(x):
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else float(x)


def snapshot_at(pre, i):
    """indicators.compute_technical_snapshot(df.iloc[:i+1]) と同じ形の dict（technical_score() にそのまま渡せる）。"""
    rows = i + 1
    ma5, ma25, ma75 = _f(pre['ma5'][i]), _f(pre['ma25'][i]), _f(pre['ma75'][i])
    last = _f(pre['close'][i])
    trend = None
    if ma5 is not None and ma25 is not None and ma75 is not None and last is not None:
        trend = 'bullish' if last > ma5 > ma25 > ma75 else 'bearish' if last < ma5 < ma25 < ma75 else 'mixed'
    elif ma5 is not None and ma25 is not None and last is not None:
        trend = 'bullish' if last > ma5 > ma25 else 'bearish' if last < ma5 < ma25 else 'mixed'
    ma = {'ma5': ma5, 'ma25': ma25, 'ma75': ma75, 'trend': trend,
          'golden_cross_recent': bool(pre['gc'][i]), 'dead_cross_recent': bool(pre['dc'][i])}
    rsi = _f(pre['rsi'][i]) if rows >= 15 else None
    if rows >= 35:
        macd = {'macd': _f(pre['macd'][i]), 'signal': _f(pre['signal'][i]), 'hist': _f(pre['hist'][i]),
                'bullish_cross_recent': bool(pre['mbc'][i]), 'bearish_cross_recent': bool(pre['mdc'][i])}
    else:
        macd = {'macd': None, 'signal': None, 'hist': None, 'bullish_cross_recent': False,
                'bearish_cross_recent': False}
    atr = _f(pre['atr'][i]) if rows >= 15 else None
    return {'ma': ma, 'rsi': rsi, 'rsi_zone': rsi_zone(rsi), 'macd': macd, 'atr': atr, 'last_close': last,
            'rows': min(rows, 220)}


def volume_surge_quiet_flags(df, pre, surge_ratio, recent_window=5, baseline_window=60, max_move_pct=3.0,
                             max_range_atr=2.0, min_baseline_value_yen=10_000_000):
    """indicators.detect_volume_surge_quiet_price() の判定を全日分まとめて行う（戻り値: bool配列）。"""
    r, b = recent_window, baseline_window
    vol = df['Volume'].astype(float)
    close = df['Close'].astype(float)
    recent = vol.rolling(r, min_periods=1).mean().to_numpy()
    base = vol.shift(r).rolling(b, min_periods=1).mean().to_numpy()
    base_close = close.shift(r + 1).rolling(b, min_periods=1).mean().to_numpy()
    c = close.to_numpy()
    hi = df['High'].astype(float).rolling(r).max().to_numpy()
    lo = df['Low'].astype(float).rolling(r).min().to_numpy()
    n = len(df)
    flags = np.zeros(n, dtype=bool)
    need = r + b + 2
    for i in range(need - 1, n):
        if not (base[i] > 0) or not (base[i - 1] > 0):
            continue
        ratio = recent[i] / base[i]
        ratio_prev = recent[i - 1] / base[i - 1]
        if not (ratio >= surge_ratio and ratio_prev < surge_ratio):
            continue
        if base_close[i] * base[i] < min_baseline_value_yen:
            continue
        start = c[i - r]
        if not start > 0:
            continue
        move = (c[i] / start - 1) * 100
        atr_before = pre['atr'][i - r]
        if abs(move) <= max_move_pct and atr_before == atr_before and (hi[i] - lo[i]) <= atr_before * max_range_atr:
            flags[i] = True
    return flags


def verify_against_production(df, sample_idx, surge_ratios=(3.0, 2.0)):
    """本番関数と一致するかの確認用。不一致の件数を返す。"""
    from indicators import compute_technical_snapshot, detect_volume_surge_quiet_price, technical_score
    pre = precompute_technicals(df)
    vflags = {s: volume_surge_quiet_flags(df, pre, s) for s in surge_ratios}
    mismatch = {'tech_score': 0, 'trend': 0, 'atr': 0, 'volume': 0}
    for i in sample_idx:
        w = df.iloc[max(0, i - 219):i + 1]
        prod = compute_technical_snapshot(w)
        fast = snapshot_at(pre, i)
        if technical_score(prod)[0] != technical_score(fast)[0]:
            mismatch['tech_score'] += 1
        if prod['ma']['trend'] != fast['ma']['trend']:
            mismatch['trend'] += 1
        if (prod['atr'] is None) != (fast['atr'] is None) or \
                (prod['atr'] and abs(prod['atr'] - fast['atr']) > 1e-6 * max(1.0, prod['atr'])):
            mismatch['atr'] += 1
        for s in surge_ratios:
            if detect_volume_surge_quiet_price(w, surge_ratio=s)['detected'] != bool(vflags[s][i]):
                mismatch['volume'] += 1
    return mismatch
