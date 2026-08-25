#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_synthetic.py — ネットワーク不要の単体テスト（合成データ）

このセッションのサンドボックスはYahoo Finance / JPXに接続できないため、実データでの
検証ができない。代わりに、意図的に「上昇トレンド」「下降トレンド」「V字」などの形を
作ったダミーのOHLCVデータを使い、各モジュールが例外を出さずに妥当な出力を返すことを
確認する。本番相当の精度検証ではなく、あくまで「壊れていないこと」の確認が目的。
"""

import numpy as np
import pandas as pd

from indicators import compute_technical_snapshot, technical_score
from sakata import detect_all_patterns, sakata_score
from scoring import composite_score, score_band_label, suggested_trade_levels
from universe import TARGET_MARKET_SEGMENTS, FALLBACK_TICKERS


def make_ohlcv(closes):
    closes = np.array(closes, dtype=float)
    highs = closes * 1.01
    lows = closes * 0.99
    opens = np.roll(closes, 1)
    opens[0] = closes[0]
    volume = np.full(len(closes), 1_000_000)
    idx = pd.date_range('2026-01-01', periods=len(closes), freq='B')
    return pd.DataFrame({'Open': opens, 'High': highs, 'Low': lows, 'Close': closes, 'Volume': volume}, index=idx)


def uptrend_df(n=120, start=1000, drift=3, noise=5, seed=1):
    rng = np.random.default_rng(seed)
    closes = start + np.cumsum(drift + rng.normal(0, noise, n))
    return make_ohlcv(closes)


def downtrend_df(n=120, start=2000, drift=-3, noise=5, seed=2):
    rng = np.random.default_rng(seed)
    closes = start + np.cumsum(drift + rng.normal(0, noise, n))
    closes = np.clip(closes, 50, None)
    return make_ohlcv(closes)


def v_shape_df(n=60, start=1500, seed=3):
    rng = np.random.default_rng(seed)
    down = start - np.cumsum(15 + rng.normal(0, 3, n // 3))
    trough = down[-1]
    up = trough + np.cumsum(14 + rng.normal(0, 3, n // 3))
    flat = up[-1] + rng.normal(0, 5, n - len(down) - len(up))
    closes = np.concatenate([down, up, flat])
    closes = np.clip(closes, 50, None)
    return make_ohlcv(closes)


def flat_short_df(n=5, start=1000):
    closes = np.full(n, start, dtype=float)
    return make_ohlcv(closes)


def run():
    print("=== indicators.py ===")
    for label, df in [('uptrend', uptrend_df()), ('downtrend', downtrend_df()),
                       ('v_shape', v_shape_df()), ('short(5日)', flat_short_df())]:
        snap = compute_technical_snapshot(df)
        score, reasons = technical_score(snap)
        print(f"[{label}] rows={snap['rows']} ma_trend={snap['ma'].get('trend')} "
              f"rsi={snap['rsi']} macd_hist={(snap['macd'] or {}).get('hist')} "
              f"atr={snap['atr']} technical_score={score}")
        assert snap['rows'] == len(df)

    print("\n=== sakata.py ===")
    for label, df in [('uptrend', uptrend_df()), ('downtrend', downtrend_df()),
                       ('v_shape', v_shape_df()), ('short(5日)', flat_short_df())]:
        patterns = detect_all_patterns(df)
        assert len(patterns) == 10, f"想定は10パターン、実際は{len(patterns)}"
        score, reasons = sakata_score(patterns)
        detected = [p['name'] for p in patterns if p['detected']]
        print(f"[{label}] detected={detected} sakata_score={score}")

    # V字パターンが「v_shape_df」で検出されやすいかの簡易確認（必ず検出される保証はないが、
    # 少なくとも例外なく動作し、スコアが0〜1の範囲に収まっていることを確認する）
    v_patterns = detect_all_patterns(v_shape_df())
    v_score, _ = sakata_score(v_patterns)
    assert 0.0 <= v_score <= 1.0

    print("\n=== scoring.py ===")
    cases = [
        dict(dividend_yield=4.0, per_pbr=18.0, eps_trend={'increasing': True, 'values': [10, 11, 12]},
             technical_score_val=0.8, sakata_score_val=0.7, growth_value=0.15),
        dict(dividend_yield=None, per_pbr=None, eps_trend=None,
             technical_score_val=None, sakata_score_val=0.0, growth_value=None),
        dict(dividend_yield=1.0, per_pbr=40.0, eps_trend={'increasing': False, 'values': [12, 11, 10]},
             technical_score_val=0.1, sakata_score_val=0.0, growth_value=-0.1),
    ]
    for i, c in enumerate(cases):
        score, breakdown, avail = composite_score(**c)
        band = score_band_label(score)
        print(f"[case{i}] score={score} band={band} avail_ratio={avail} breakdown={breakdown}")
        if score is not None:
            assert 0 <= score <= 100

    trade = suggested_trade_levels(1000.0, 20.0)
    print(f"trade_levels(atr有): {trade}")
    assert trade['initial_stop'] < 1000.0

    trade_no_atr = suggested_trade_levels(1000.0, None)
    print(f"trade_levels(atrなし): {trade_no_atr}")
    assert trade_no_atr['initial_stop'] < 1000.0

    print("\n=== universe.py（定数のみ確認。ネットワークは叩かない） ===")
    print(f"TARGET_MARKET_SEGMENTS={TARGET_MARKET_SEGMENTS}")
    print(f"FALLBACK_TICKERS={FALLBACK_TICKERS}")
    assert len(TARGET_MARKET_SEGMENTS) == 3
    assert len(FALLBACK_TICKERS) == 5

    print("\n全テストがエラーなく完了しました（合成データのみ・実データ未検証）")


if __name__ == '__main__':
    run()
