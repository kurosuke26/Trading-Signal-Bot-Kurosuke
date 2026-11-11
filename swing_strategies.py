#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
swing_strategies.py — スイングトレード用スクリーニング（2026-11-11追加。本番LONG/SHORTとは別枠）。

数日〜1か月（5〜20営業日）で利益を狙う3つの型。ルールは検証の前に決めたもので、検証結果を見て
条件を足したり絞ったりはしない（偶然よく見える条件を選んでしまうのを防ぐため）。
変えてよいのは「手仕舞いの方法」と「地合いフィルターの有無」だけで、それも evaluate_swing.py の
walk-forward（その時点より前のデータだけで選ぶ）で決める。

【型① 上昇トレンドの押し目買い（PULLBACK）】
  上昇トレンド：終値 > 75日線、25日線 > 75日線、75日線が上向き（20営業日前より上）、120日の上昇率 +10%以上
  押し目      ：直近5日で −3%以上下げて、終値が25日線の −3%〜+3% にあり、RSI(14) が50以下
  反発の確認  ：当日の終値が前日の終値より上
  並べ順      ：120日の上昇率が大きい順

【型② 高値ブレイク（BREAKOUT）】
  終値が直近60営業日の高値（当日を除く）を上抜け、出来高が直近20日平均の2倍以上、
  終値 > 25日線 > 75日線、終値が25日線の +15% 以内（上がりすぎは追わない）
  並べ順：出来高の倍率が大きい順

【型③ 上方修正後の継続（REVISION）】
  会社の業績予想（営業利益。無ければ経常→純利益）が、同じ期の前回予想から +5%以上 上方修正された開示の
  「反応日」（15:30までの開示はその日、それ以降は翌営業日）に、終値が前日比 +2%以上・出来高が直近20日平均の
  1.5倍以上・終値 > 25日線
  並べ順：反応日の上昇率が大きい順
  ※本番（毎日の通知）は J-Quants Free が12週間遅れのため、TDnet の開示タイトルに「上方修正」を含むもので判定する
   （修正率 +5% は確かめられない）。検証は J-Quants の業績予想の数値で判定する。

【共通】
  - シグナルはその日の引けまでのデータだけで判定し、翌営業日の始値で買う（本番の他の仮想売買と同じ）
  - 取引できない銘柄（売買代金不足・仕手株化の兆候）は除く
  - 1つの型で1日あたり上位 TOP_N 件まで。保有中の銘柄には同じ型で重ねて入らない
  - 地合いフィルター（オン/オフを検証で選ぶ）：市場平均（取引可能な全銘柄の等金額指数）が自身の75日線より上の日だけ買う
  - 手仕舞い：保有期間の上限（5／10／20営業日目の引け）と、ストップ（ATR×2／ATR×3 の切り上げ、または
    終値が25日線を割ったら翌営業日の始値）の早いほう
"""

import numpy as np
import pandas as pd

PULLBACK, BREAKOUT, REVISION = 'PULLBACK', 'BREAKOUT', 'REVISION'
TYPES = (PULLBACK, BREAKOUT, REVISION)
TYPE_LABEL = {PULLBACK: '① 上昇トレンドの押し目買い', BREAKOUT: '② 高値ブレイク', REVISION: '③ 上方修正後の継続'}
TOP_N = 5

HOLD_GRID = (5, 10, 20)
STOP_GRID = ('atr2', 'atr3', 'ma25')
STOP_LABEL = {'atr2': 'ATR×2', 'atr3': 'ATR×3', 'ma25': '25日線割れ'}
REGIME_GRID = (False, True)
REVISION_MIN_PCT = 5.0
DISCLOSURE_CUTOFF = '15:30'  # 東証の大引け（2024-11-05以降）


# ---------------------------------------------------------------------------
# 特徴量とシグナル（検証・本番で共通）
# ---------------------------------------------------------------------------

def compute_features(df):
    """df: Open/High/Low/Close/Volume の日足（日付の昇順）。各日の値は、その日の引けまでのデータだけで決まる。"""
    c = df['Close'].astype(float)
    h = df['High'].astype(float)
    lo = df['Low'].astype(float)
    vol = df['Volume'].astype(float)
    f = pd.DataFrame(index=df.index)
    f['close'] = c
    f['ma25'] = c.rolling(25).mean()
    f['ma75'] = c.rolling(75).mean()
    f['ma75_up'] = f['ma75'] > f['ma75'].shift(20)
    prev = c.shift(1)
    tr = pd.concat([h - lo, (h - prev).abs(), (lo - prev).abs()], axis=1).max(axis=1)
    f['atr'] = tr.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    delta = c.diff()
    g = delta.clip(lower=0).ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    ls = (-delta.clip(upper=0)).ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    f['rsi'] = (100 - 100 / (1 + g / ls.replace(0, np.nan))).where(ls != 0, 100.0)
    f['ret1'] = c / prev - 1
    f['ret5'] = c / c.shift(5) - 1
    f['ret120'] = c / c.shift(120) - 1
    f['vol_ratio'] = vol / vol.shift(1).rolling(20).mean()
    f['high60_prev'] = h.shift(1).rolling(60).max()
    return f


def pullback_mask(f):
    return ((f['close'] > f['ma75']) & (f['ma25'] > f['ma75']) & f['ma75_up'] & (f['ret120'] >= 0.10)
            & (f['ret5'] <= -0.03) & (f['close'] >= f['ma25'] * 0.97) & (f['close'] <= f['ma25'] * 1.03)
            & (f['rsi'] <= 50) & (f['ret1'] > 0)).fillna(False)


def breakout_mask(f):
    return ((f['close'] > f['high60_prev']) & (f['vol_ratio'] >= 2.0) & (f['close'] > f['ma25'])
            & (f['ma25'] > f['ma75']) & (f['close'] <= f['ma25'] * 1.15)).fillna(False)


def revision_mask(f, reaction):
    """reaction: 反応日なら True の bool Series（f と同じ index）。"""
    return (reaction & (f['ret1'] >= 0.02) & (f['vol_ratio'] >= 1.5) & (f['close'] > f['ma25'])).fillna(False)


def rank_key(kind, f):
    """1日に候補が多いときの並べ順（大きいほど上位）。"""
    return {PULLBACK: f['ret120'], BREAKOUT: f['vol_ratio'], REVISION: f['ret1']}[kind]


def reaction_dates(trading_dates, disclosures):
    """
    disclosures: [(開示日 'YYYY-MM-DD', 開示時刻 'HH:MM' or '')]。15:30までの開示はその日、それ以降（または時刻不明）は
    翌営業日を反応日とする。戻り値：反応日（trading_dates の要素）の集合。
    """
    out = set()
    td = list(trading_dates)
    for d, t in disclosures:
        same_day_ok = bool(t) and t[:5] < DISCLOSURE_CUTOFF
        k = np.searchsorted(td, d, side='left' if same_day_ok else 'right')
        if k < len(td):
            out.add(td[k])
    return out


# ---------------------------------------------------------------------------
# 手仕舞い（検証・本番で共通）
# ---------------------------------------------------------------------------

def walk_position(O, C, ma25, atr, e, hold, stop):
    """
    e日の始値で約定したポジションを、データの最後まで進める（検証・本番で共通の手仕舞いルール）。
      - 保有期間の上限：約定日を1日目として hold 日目の引け
      - ストップ：ATR×m は「終値 − ATR×m」の切り上げ（初期値は 約定値 − 前日ATR×m）。終値が抵触したら翌営業日の始値
                 25日線割れ：終値が25日線を下回ったら翌営業日の始値
      同じ日に両方なら引け（保有期間の上限）を優先する（そのほうが早い）。
    戻り値 dict:
      state='closed'         : x（決済日のindex）, at_close（True＝x日の終値／False＝x日の始値）
      state='exit_next_open' : 最終日の終値でストップに抵触（次の営業日の始値で手仕舞い）
      state='open'           : days_held（最終日までの保有日数）, stop_level（ATR型の現在のストップ。25日線型は25日線）
    """
    n = len(C)
    m = {'atr2': 2.0, 'atr3': 3.0}.get(stop)
    a0 = atr[e - 1] if e >= 1 and atr[e - 1] == atr[e - 1] else None
    level = O[e] - a0 * m if (m and a0) else -np.inf
    for j in range(e, n):
        if j - e + 1 >= hold:
            return {'state': 'closed', 'x': j, 'at_close': True}
        if m:
            if atr[j] == atr[j]:
                level = max(level, C[j] - atr[j] * m)
            hit = C[j] <= level
        else:
            hit = ma25[j] == ma25[j] and C[j] < ma25[j]
        if hit:
            if j + 1 < n:
                return {'state': 'closed', 'x': j + 1, 'at_close': False}
            return {'state': 'exit_next_open'}
    cur = level if m else (ma25[n - 1] if n else None)
    return {'state': 'open', 'days_held': n - e, 'stop_level': None if cur is None or not np.isfinite(cur) else float(cur)}


def exit_index(O, C, ma25, atr, e, hold, stop):
    """walk_position の決済日だけを返す。(x, at_close)、まだ決済していなければ (None, None)。"""
    r = walk_position(O, C, ma25, atr, e, hold, stop)
    return (r['x'], r['at_close']) if r['state'] == 'closed' else (None, None)
