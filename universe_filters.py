#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
universe_filters.py — 「実際に売買できる銘柄か」を日ごとに判定する共通フィルター（2026-09-16追加）。

検証スクリプト（evaluate_sakata.py / evaluate_strategy.py / evaluate_investor_styles.py）で共通に使う。
すべて t日の引けまでに分かる値だけで判定する（未来の値は使わない）。株価の上限（最低投資金額など）は
条件に含めない。

【除外する銘柄】
1. 売買代金が低すぎて売買が成立しにくい
   - 直近20日の平均売買代金が MIN_AVG_TRADING_VALUE_YEN 未満
   - 直近60日のうち売買が成立しなかった日（出来高0・四本値なし）が MAX_NO_TRADE_DAYS_60 日以上
2. 仕手株化している（どれか1つでも該当）
   - 売買代金の急膨張：直近20日平均が、その前250日平均の SURGE_VALUE_RATIO 倍以上
   - 株価の異常な振れ：直近60日の高値÷安値が PRICE_RANGE_RATIO_60 倍以上
   - ボラティリティ異常：ATR(14)÷終値が MAX_ATR_PCT 以上
   - 値幅制限の頻発：直近20日でストップ高・ストップ安（J-Quants UL/LL フラグ）が MAX_LIMIT_HITS_20 回以上

閾値はモジュール定数。変更した場合は検証結果の出力（パラメータ欄）にも自動で記録される。
"""

import numpy as np
import pandas as pd

MIN_AVG_TRADING_VALUE_YEN = 50_000_000
MAX_NO_TRADE_DAYS_60 = 3
SURGE_VALUE_RATIO = 10.0
PRICE_RANGE_RATIO_60 = 3.0
MAX_ATR_PCT = 0.10
MAX_LIMIT_HITS_20 = 3


def filter_params():
    return {
        'min_avg_trading_value_yen_20d': MIN_AVG_TRADING_VALUE_YEN,
        'max_no_trade_days_60d': MAX_NO_TRADE_DAYS_60,
        'surge_trading_value_ratio_20d_vs_250d': SURGE_VALUE_RATIO,
        'price_high_low_ratio_60d': PRICE_RANGE_RATIO_60,
        'max_atr_pct': MAX_ATR_PCT,
        'max_limit_hits_20d': MAX_LIMIT_HITS_20,
    }


def tradeable_flags(raw_rows_df, df, atr):
    """
    raw_rows_df: bars.json をそのまま DataFrame にしたもの（売買不成立の日も含む。Date昇順・Date列あり）
    df: evaluate_sakata.load_adjusted_bars() の戻り値（売買成立日のみ・Date index）
    atr: df と同じ長さの ATR 配列
    戻り値: (tradeable: bool配列, reason: 文字列配列)  いずれも df の行に対応
    """
    n = len(df)
    reasons = np.array([''] * n, dtype=object)

    # --- 売買不成立日の数（暦ではなく J-Quants の営業日行ベース） ---
    raw = raw_rows_df.copy()
    raw['Date'] = pd.to_datetime(raw['Date'])
    raw = raw.sort_values('Date').set_index('Date')
    vo = pd.to_numeric(raw.get('Vo'), errors='coerce').fillna(0)
    no_trade = ((vo <= 0) | pd.to_numeric(raw.get('C'), errors='coerce').isna()).astype(int)
    no_trade_60 = no_trade.rolling(60, min_periods=1).sum().reindex(df.index).to_numpy()
    ul = (raw.get('UL').astype(str) == '1').astype(int) if 'UL' in raw else pd.Series(0, index=raw.index)
    ll = (raw.get('LL').astype(str) == '1').astype(int) if 'LL' in raw else pd.Series(0, index=raw.index)
    limit_hits_20 = (ul + ll).rolling(20, min_periods=1).sum().reindex(df.index).to_numpy()

    value = df['Value'].astype(float)
    v20 = value.rolling(20, min_periods=20).mean().to_numpy()
    v250_prior = value.shift(20).rolling(250, min_periods=60).mean().to_numpy()
    hi60 = df['High'].astype(float).rolling(60, min_periods=20).max().to_numpy()
    lo60 = df['Low'].astype(float).rolling(60, min_periods=20).min().to_numpy()
    close = df['Close'].astype(float).to_numpy()

    ok = np.ones(n, dtype=bool)

    def mark(cond, label):
        nonlocal ok
        cond = np.asarray(cond, dtype=bool)
        for i in np.flatnonzero(cond & ok):
            reasons[i] = label
        ok &= ~cond

    with np.errstate(invalid='ignore', divide='ignore'):
        mark(~(v20 >= MIN_AVG_TRADING_VALUE_YEN), '売買代金不足')
        mark(no_trade_60 >= MAX_NO_TRADE_DAYS_60, '売買不成立日が多い')
        mark((v250_prior > 0) & (v20 / v250_prior >= SURGE_VALUE_RATIO), '仕手化:売買代金急膨張')
        mark((lo60 > 0) & (hi60 / lo60 >= PRICE_RANGE_RATIO_60), '仕手化:株価の異常な振れ')
        mark(np.asarray(atr) / close >= MAX_ATR_PCT, '仕手化:ボラティリティ異常')
        mark(limit_hits_20 >= MAX_LIMIT_HITS_20, '仕手化:値幅制限の頻発')
    return ok, reasons
