#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
indicators.py — テクニカル指標の計算モジュール（Phase 2）

対応指標:
- 移動平均線（SMA5 / SMA25 / SMA75）とゴールデンクロス・デッドクロス判定
- RSI（14日）
- MACD（12, 26, 9）
- ATR（14日）— トレーリングストップ幅の計算に使用

入力は yfinance の history() が返す DataFrame（Open/High/Low/Close/Volume 列を持つ）を想定。
すべて pandas/numpy のみで計算しており、外部ライブラリ（ta-lib 等）には依存しない
（GitHub Actions 側でのインストールを増やさないための方針）。

【注意】
本モジュールの関数は「直近データが十分にあること」を前提にしている。
データ件数が指標の計算に必要な最小日数に満たない場合は、該当指標は None を返す
（呼び出し側は None を「判定不能」として扱うこと。0 や中立値で埋めて誤解を招かないため）。
"""

import numpy as np
import pandas as pd


MIN_ROWS_FOR_MA75 = 75
MIN_ROWS_FOR_MACD = 35  # EMA26 + シグナルEMA9 が安定するのに必要な目安
MIN_ROWS_FOR_RSI = 15
MIN_ROWS_FOR_ATR = 15


def _safe_last(series):
    if series is None or len(series) == 0:
        return None
    val = series.iloc[-1]
    if pd.isna(val):
        return None
    return float(val)


def compute_sma(df, window):
    """単純移動平均。データ不足時はNoneを返す。"""
    if df is None or len(df) < window:
        return None, None
    sma = df['Close'].rolling(window=window).mean()
    return sma, _safe_last(sma)


def compute_moving_averages(df):
    """
    MA5 / MA25 / MA75 と、直近のクロス状況をまとめて返す。

    戻り値:
      {
        'ma5': float or None, 'ma25': float or None, 'ma75': float or None,
        'trend': 'bullish' | 'bearish' | 'mixed' | None,   # 価格 > MA5 > MA25 > MA75 なら bullish
        'golden_cross_recent': bool,   # 直近3営業日以内にMA5がMA25を上抜け
        'dead_cross_recent': bool,     # 直近3営業日以内にMA5がMA25を下抜け
      }
    """
    result = {
        'ma5': None, 'ma25': None, 'ma75': None,
        'trend': None, 'golden_cross_recent': False, 'dead_cross_recent': False,
    }
    if df is None or df.empty:
        return result

    sma5_series, ma5 = compute_sma(df, 5)
    sma25_series, ma25 = compute_sma(df, 25)
    sma75_series, ma75 = compute_sma(df, 75)
    result['ma5'], result['ma25'], result['ma75'] = ma5, ma25, ma75

    last_close = _safe_last(df['Close'])

    if ma5 is not None and ma25 is not None and ma75 is not None and last_close is not None:
        if last_close > ma5 > ma25 > ma75:
            result['trend'] = 'bullish'
        elif last_close < ma5 < ma25 < ma75:
            result['trend'] = 'bearish'
        else:
            result['trend'] = 'mixed'
    elif ma5 is not None and ma25 is not None and last_close is not None:
        if last_close > ma5 > ma25:
            result['trend'] = 'bullish'
        elif last_close < ma5 < ma25:
            result['trend'] = 'bearish'
        else:
            result['trend'] = 'mixed'

    if sma5_series is not None and sma25_series is not None:
        diff = (sma5_series - sma25_series).dropna()
        if len(diff) >= 4:
            recent = diff.iloc[-4:]
            sign_changes = np.sign(recent.iloc[:-1].values) != np.sign(recent.iloc[1:].values)
            if sign_changes.any():
                # 直近3日以内で符号が反転した箇所を探す
                idx_changed = np.where(sign_changes)[0]
                last_change_idx = idx_changed[-1]
                went_positive = recent.iloc[last_change_idx + 1] > 0
                if went_positive:
                    result['golden_cross_recent'] = True
                else:
                    result['dead_cross_recent'] = True

    return result


def compute_rsi(df, period=14):
    """
    RSI（Wilder方式の平滑化）。
    戻り値: (rsi_series, 最新値 or None)
    """
    if df is None or len(df) < period + 1:
        return None, None

    close = df['Close']
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    # 平均下落幅が0（ずっと上昇のみ）の場合はRSI=100として扱う
    rsi = rsi.where(avg_loss != 0, 100.0)

    return rsi, _safe_last(rsi)


def compute_macd(df, fast=12, slow=26, signal=9):
    """
    MACD。
    戻り値:
      {
        'macd': float or None, 'signal': float or None, 'hist': float or None,
        'bullish_cross_recent': bool,  # 直近3営業日以内にMACDがシグナルを上抜け
        'bearish_cross_recent': bool,
      }
    """
    result = {'macd': None, 'signal': None, 'hist': None,
              'bullish_cross_recent': False, 'bearish_cross_recent': False}
    if df is None or len(df) < slow + signal:
        return result

    close = df['Close']
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line

    result['macd'] = _safe_last(macd_line)
    result['signal'] = _safe_last(signal_line)
    result['hist'] = _safe_last(hist)

    diff = (macd_line - signal_line).dropna()
    if len(diff) >= 4:
        recent = diff.iloc[-4:]
        sign_changes = np.sign(recent.iloc[:-1].values) != np.sign(recent.iloc[1:].values)
        if sign_changes.any():
            idx_changed = np.where(sign_changes)[0]
            last_change_idx = idx_changed[-1]
            went_positive = recent.iloc[last_change_idx + 1] > 0
            if went_positive:
                result['bullish_cross_recent'] = True
            else:
                result['bearish_cross_recent'] = True

    return result


def compute_atr(df, period=14):
    """
    ATR（Wilder方式）。トレーリングストップ幅の計算に使用。
    戻り値: (atr_series, 最新値 or None)
    """
    if df is None or len(df) < period + 1:
        return None, None

    high, low, close = df['High'], df['Low'], df['Close']
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    atr = tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    return atr, _safe_last(atr)


def rsi_zone(rsi_value):
    """RSIの状態をラベル化。買われすぎ/売られすぎ/中立/None。"""
    if rsi_value is None:
        return None
    if rsi_value >= 70:
        return 'overbought'
    if rsi_value <= 30:
        return 'oversold'
    return 'neutral'


def compute_technical_snapshot(df):
    """
    1銘柄分のOHLCV DataFrameから、判定に必要なテクニカル指標一式をまとめて計算する。
    データ不足の指標は None のまま返すので、呼び出し側で欠損を考慮すること。
    """
    ma = compute_moving_averages(df)
    _, rsi_val = compute_rsi(df)
    macd = compute_macd(df)
    _, atr_val = compute_atr(df)

    return {
        'ma': ma,
        'rsi': rsi_val,
        'rsi_zone': rsi_zone(rsi_val),
        'macd': macd,
        'atr': atr_val,
        'last_close': _safe_last(df['Close']) if df is not None and not df.empty else None,
        'rows': 0 if df is None else len(df),
    }


def technical_score(snapshot):
    """
    テクニカル指標から0〜1のスコア（README仕様の「テクニカル：25%」に対応する係数）を算出する。

    配点の考え方（合計1.0）:
      - MA配列（価格>MA5>MA25>MA75）が強気     : 0.4
      - MA5がMA25を直近ゴールデンクロス        : 0.2（強気トレンドと重複可）
      - MACDが直近でシグナルを上抜け、またはヒストグラムが正 : 0.25
      - RSIが50〜70（過熱していない上昇モメンタム）か、
        30近辺からの反発（oversoldからneutralへの回復)      : 0.15

    データ欠損がある項目は加点せず（0点扱い）、加点対象外である旨を
    'available' に記録する。全項目欠損の場合は None を返す（判定不能）。
    """
    if snapshot is None:
        return None, []

    ma = snapshot.get('ma') or {}
    macd = snapshot.get('macd') or {}
    rsi_val = snapshot.get('rsi')

    any_available = False
    score = 0.0
    reasons = []

    if ma.get('trend') is not None:
        any_available = True
        if ma['trend'] == 'bullish':
            score += 0.4
            reasons.append('MA配列が強気（価格>MA5>MA25>MA75）')
        elif ma['trend'] == 'bearish':
            reasons.append('MA配列が弱気（価格<MA5<MA25<MA75）')

    if ma.get('golden_cross_recent'):
        any_available = True
        score += 0.2
        reasons.append('MA5がMA25を直近ゴールデンクロス')
    elif ma.get('dead_cross_recent'):
        any_available = True
        reasons.append('MA5がMA25を直近デッドクロス')

    if macd.get('macd') is not None:
        any_available = True
        if macd.get('bullish_cross_recent'):
            score += 0.25
            reasons.append('MACDが直近でシグナルを上抜け（ゴールデンクロス）')
        elif macd.get('hist') is not None and macd['hist'] > 0:
            score += 0.15
            reasons.append('MACDヒストグラムがプラス（買い優勢）')
        elif macd.get('bearish_cross_recent'):
            reasons.append('MACDが直近でシグナルを下抜け（デッドクロス）')

    if rsi_val is not None:
        any_available = True
        if 50 <= rsi_val <= 70:
            score += 0.15
            reasons.append(f'RSI {rsi_val:.1f}（過熱感の少ない上昇モメンタム）')
        elif rsi_val < 30:
            reasons.append(f'RSI {rsi_val:.1f}（売られすぎ圏。反発待ち）')
        elif rsi_val > 70:
            reasons.append(f'RSI {rsi_val:.1f}（買われすぎ圏。過熱注意）')

    if not any_available:
        return None, []

    return min(score, 1.0), reasons
