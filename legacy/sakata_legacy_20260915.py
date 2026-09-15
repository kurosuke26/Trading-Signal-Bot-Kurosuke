#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sakata.py — 酒田五法 10パターンの検出モジュール（Phase 2）

引き継ぎ資料（IMPLEMENTATION_STRATEGY.md / README.md）に記載されていた10パターンを、
日足OHLCVデータ（pandas DataFrame、Open/High/Low/Close列）から検出する。

【重要な注意】
- 各パターンの「精度（%）」は引き継ぎ資料に記載されていた数値をそのまま参考値として
  使っており、本セッションで実データを使ってバックテスト・検証した実測値ではない。
  Discord投稿やコード内では「仕様書に基づく参考値」であることが伝わるようにしている。
- 酒田五法の古典的な定義には解釈の幅があり、特に「かい値」は資料内の一般的な用語と
  一致しないため、資料の説明（"反発シグナル"）から近いとみなした「毛抜き底
  （つい先日の安値とほぼ同水準で下げ止まり、反発した2本組の形）」として実装している。
  他のパターンも典型的な定義に基づく実装であり、完全に一意な検出ロジックがあるわけ
  ではない（同じ「三山」でも高値・安値の許容誤差の取り方で判定が変わりうる）。
- 検出は直近の値動きに対するヒューリスティック（経験則）であり、100%の精度を
  保証するものではない。あくまで参考シグナルの一つとして、テクニカル指標
  （MA/RSI/MACD）や配当・PERなどのファンダメンタルズと合わせて総合判断すること。

各検出関数は以下の形式の dict を返す:
  {
    'name': '三山', 'detected': bool, 'direction': 'bullish' | 'bearish' | None,
    'reference_accuracy': 80,  # 資料記載の参考精度(%)。実測値ではない
    'note': '...',             # 検出理由の簡単な説明（Noneの場合あり）
  }
"""

import numpy as np
import pandas as pd


REFERENCE_ACCURACY = {
    '三山': 80, '三川': 85, '三兵': 70, '三空': 75, '三法': 65,
    'たすき線': 60, '毛抜き（かい値）': 70, '段違い': 55, 'N字': 75, 'V字': 80,
}


def _empty(name, note=None):
    return {'name': name, 'detected': False, 'direction': None,
            'reference_accuracy': REFERENCE_ACCURACY.get(name), 'note': note}


def _find_local_extrema(series, order=3):
    """
    単純な近傍比較による極大・極小の検出（scipyに依存しないための自前実装）。
    order: 前後何本と比較するか。
    戻り値: (peak_idx_list, trough_idx_list) — series の位置インデックス（0始まり）
    """
    values = series.values
    n = len(values)
    peaks, troughs = [], []
    for i in range(order, n - order):
        window = values[i - order:i + order + 1]
        center = values[i]
        if center == window.max() and np.argmax(window) == order:
            peaks.append(i)
        if center == window.min() and np.argmin(window) == order:
            troughs.append(i)
    return peaks, troughs


def detect_sanzan(df, tolerance=0.03):
    """三山（三尊天井に近い形）: 直近で高値がほぼ同水準の山が3つ並び、直近終値が
    2つの谷を結ぶネックラインを割り込んでいれば弱気の売りシグナルとみなす。"""
    name = '三山'
    if df is None or len(df) < 30:
        return _empty(name, 'データ不足')

    recent = df.tail(60)
    peaks, troughs = _find_local_extrema(recent['High'], order=3)
    if len(peaks) < 3:
        return _empty(name)

    p1, p2, p3 = peaks[-3], peaks[-2], peaks[-1]
    h1, h2, h3 = recent['High'].iloc[p1], recent['High'].iloc[p2], recent['High'].iloc[p3]
    avg_h = (h1 + h2 + h3) / 3
    if max(abs(h1 - avg_h), abs(h2 - avg_h), abs(h3 - avg_h)) / avg_h > tolerance:
        return _empty(name)

    between_troughs = [t for t in troughs if p1 < t < p3]
    if len(between_troughs) < 2:
        return _empty(name)
    neckline = recent['Low'].iloc[between_troughs].min()
    last_close = recent['Close'].iloc[-1]

    detected = last_close < neckline
    note = f'高値3つがほぼ同水準（{h1:.0f}/{h2:.0f}/{h3:.0f}）でネックライン{neckline:.0f}を' + \
           ('割り込み済み' if detected else '維持中（ネックライン割れ待ち）')
    return {'name': name, 'detected': detected, 'direction': 'bearish' if detected else None,
            'reference_accuracy': REFERENCE_ACCURACY[name], 'note': note}


def detect_sankawa(df, tolerance=0.03):
    """三川（三川底に近い形）: 三山の逆。安値がほぼ同水準の谷が3つ並び、
    直近終値が2つの山を結ぶレジスタンスラインを上抜けていれば強気の買いシグナル。"""
    name = '三川'
    if df is None or len(df) < 30:
        return _empty(name, 'データ不足')

    recent = df.tail(60)
    peaks, troughs = _find_local_extrema(recent['Low'], order=3)
    if len(troughs) < 3:
        return _empty(name)

    t1, t2, t3 = troughs[-3], troughs[-2], troughs[-1]
    l1, l2, l3 = recent['Low'].iloc[t1], recent['Low'].iloc[t2], recent['Low'].iloc[t3]
    avg_l = (l1 + l2 + l3) / 3
    if max(abs(l1 - avg_l), abs(l2 - avg_l), abs(l3 - avg_l)) / avg_l > tolerance:
        return _empty(name)

    between_peaks = [p for p in peaks if t1 < p < t3]
    if len(between_peaks) < 2:
        return _empty(name)
    resistance = recent['High'].iloc[between_peaks].max()
    last_close = recent['Close'].iloc[-1]

    detected = last_close > resistance
    note = f'安値3つがほぼ同水準（{l1:.0f}/{l2:.0f}/{l3:.0f}）でレジスタンス{resistance:.0f}を' + \
           ('上抜け済み' if detected else '未達（レジスタンス突破待ち）')
    return {'name': name, 'detected': detected, 'direction': 'bullish' if detected else None,
            'reference_accuracy': REFERENCE_ACCURACY[name], 'note': note}


def detect_sanpei(df):
    """三兵: 直近3本が同方向の陽線（赤三兵）または陰線（黒三兵）で、
    それぞれ高値・安値を切り上げ／切り下げている場合に検出。"""
    name = '三兵'
    if df is None or len(df) < 3:
        return _empty(name, 'データ不足')

    last3 = df.tail(3)
    o, c, h, l = last3['Open'].values, last3['Close'].values, last3['High'].values, last3['Low'].values

    bullish = all(c[i] > o[i] for i in range(3)) and all(c[i] > c[i - 1] for i in range(1, 3)) \
        and all(l[i] > l[i - 1] for i in range(1, 3))
    bearish = all(c[i] < o[i] for i in range(3)) and all(c[i] < c[i - 1] for i in range(1, 3)) \
        and all(h[i] < h[i - 1] for i in range(1, 3))

    if bullish:
        return {'name': name, 'detected': True, 'direction': 'bullish',
                'reference_accuracy': REFERENCE_ACCURACY[name], 'note': '赤三兵（陽線3本で高値・安値切り上げ）'}
    if bearish:
        return {'name': name, 'detected': True, 'direction': 'bearish',
                'reference_accuracy': REFERENCE_ACCURACY[name], 'note': '黒三兵（陰線3本で高値・安値切り下げ）'}
    return _empty(name)


def detect_sanku(df):
    """三空: 直近4本の間に同方向のギャップ（窓）が3回連続で発生している場合。
    強いトレンドの継続シグナルだが、3つ目の窓は「行き過ぎ」で反転前兆とされることも多い。"""
    name = '三空'
    if df is None or len(df) < 4:
        return _empty(name, 'データ不足')

    last4 = df.tail(4)
    highs, lows = last4['High'].values, last4['Low'].values

    gap_up = all(lows[i] > highs[i - 1] for i in range(1, 4))
    gap_down = all(highs[i] < lows[i - 1] for i in range(1, 4))

    if gap_up:
        return {'name': name, 'detected': True, 'direction': 'bearish',
                'reference_accuracy': REFERENCE_ACCURACY[name],
                'note': '上放れ窓が3連続 → 上げすぎ（買われすぎ）で反落警戒'}
    if gap_down:
        return {'name': name, 'detected': True, 'direction': 'bullish',
                'reference_accuracy': REFERENCE_ACCURACY[name],
                'note': '下放れ窓が3連続 → 下げすぎ（売られすぎ）で反発期待'}
    return _empty(name)


def detect_sanpou(df, tolerance=0.4):
    """
    三法（上げ三法/下げ三法）: 大陽線（または大陰線）の後、その値幅の中に収まる
    小さいローソク足が2〜3本続き、その後に元のトレンド方向へ大きく抜ける足が
    出た場合をトレンド継続シグナルとする。
    """
    name = '三法'
    if df is None or len(df) < 5:
        return _empty(name, 'データ不足')

    last5 = df.tail(5)
    lead = last5.iloc[0]
    middle = last5.iloc[1:4]
    breakout = last5.iloc[4]

    lead_range = lead['High'] - lead['Low']
    if lead_range <= 0:
        return _empty(name)

    lead_bullish = lead['Close'] > lead['Open']
    contained = ((middle['High'] <= lead['High']) & (middle['Low'] >= lead['Low'])).all()
    small_bodies = ((middle['High'] - middle['Low']) <= lead_range * tolerance).all()

    if not (contained and small_bodies):
        return _empty(name)

    if lead_bullish and breakout['Close'] > lead['High']:
        return {'name': name, 'detected': True, 'direction': 'bullish',
                'reference_accuracy': REFERENCE_ACCURACY[name],
                'note': '上げ三法：大陽線後の保ち合いを上に抜けてトレンド継続'}
    if (not lead_bullish) and breakout['Close'] < lead['Low']:
        return {'name': name, 'detected': True, 'direction': 'bearish',
                'reference_accuracy': REFERENCE_ACCURACY[name],
                'note': '下げ三法：大陰線後の保ち合いを下に抜けてトレンド継続'}
    return _empty(name, '大陽線/大陰線後の保ち合いは検出したが、ブレイク方向が未確定')


def detect_tasuki(df):
    """たすき線（上げたすき/下げたすき）: トレンド方向への窓開けの翌日、
    逆方向の足が出て寄り付きが前日の実体内に食い込むが、窓は埋めきらない形。継続シグナル。"""
    name = 'たすき線'
    if df is None or len(df) < 2:
        return _empty(name, 'データ不足')

    last2 = df.tail(2)
    d1, d2 = last2.iloc[0], last2.iloc[1]

    up_gap = d1['Close'] > d1['Open'] and d2['Open'] > d1['Close']
    if up_gap and d2['Close'] < d2['Open'] and d2['Close'] > d1['Open'] and d2['Low'] > d1['High']:
        return {'name': name, 'detected': True, 'direction': 'bullish',
                'reference_accuracy': REFERENCE_ACCURACY[name],
                'note': '上げたすき線：上放れ後の陰線が窓を埋めず上昇継続を示唆'}

    down_gap = d1['Close'] < d1['Open'] and d2['Open'] < d1['Close']
    if down_gap and d2['Close'] > d2['Open'] and d2['Close'] < d1['Open'] and d2['High'] < d1['Low']:
        return {'name': name, 'detected': True, 'direction': 'bearish',
                'reference_accuracy': REFERENCE_ACCURACY[name],
                'note': '下げたすき線：下放れ後の陽線が窓を埋めず下落継続を示唆'}
    return _empty(name)


def detect_kenuki(df, tolerance=0.005):
    """
    毛抜き（資料内の「かい値」に相当する反発シグナルとして実装。詳細は本ファイル冒頭の注記を参照）。
    直近2本の安値（高値）がほぼ同水準で並び、下げ止まり／上げ止まりを示す形。
    """
    name = '毛抜き（かい値）'
    if df is None or len(df) < 2:
        return _empty(name, 'データ不足')

    last2 = df.tail(2)
    d1, d2 = last2.iloc[0], last2.iloc[1]

    if d1['Low'] > 0 and abs(d1['Low'] - d2['Low']) / d1['Low'] <= tolerance and d2['Close'] > d2['Open']:
        return {'name': name, 'detected': True, 'direction': 'bullish',
                'reference_accuracy': REFERENCE_ACCURACY[name],
                'note': f'毛抜き底：安値が2本連続でほぼ同水準（{d1["Low"]:.0f}/{d2["Low"]:.0f}）から陽線で反発'}

    if d1['High'] > 0 and abs(d1['High'] - d2['High']) / d1['High'] <= tolerance and d2['Close'] < d2['Open']:
        return {'name': name, 'detected': True, 'direction': 'bearish',
                'reference_accuracy': REFERENCE_ACCURACY[name],
                'note': f'毛抜き天井：高値が2本連続でほぼ同水準（{d1["High"]:.0f}/{d2["High"]:.0f}）から陰線で反落'}
    return _empty(name)


def detect_danchigai(df, min_steps=3):
    """
    段違い: 同方向のローソク足が連続し、かつ各日の始値が前日終値より
    さらにトレンド方向へ離れて始まる（＝寄り付きが階段状にずれていく）形。
    三空ほど極端な窓は開かないが、勢いよく一方向に進んでいる状態を継続シグナルとして検出。
    """
    name = '段違い'
    if df is None or len(df) < min_steps + 1:
        return _empty(name, 'データ不足')

    last = df.tail(min_steps + 1)
    closes = last['Close'].values
    opens = last['Open'].values

    up_steps = all(closes[i] > opens[i] for i in range(1, len(last))) and \
        all(opens[i] > closes[i - 1] for i in range(1, len(last)))
    down_steps = all(closes[i] < opens[i] for i in range(1, len(last))) and \
        all(opens[i] < closes[i - 1] for i in range(1, len(last)))

    if up_steps:
        return {'name': name, 'detected': True, 'direction': 'bullish',
                'reference_accuracy': REFERENCE_ACCURACY[name],
                'note': f'陽線が{min_steps}本連続で寄り付きが切り上がる段違い上昇'}
    if down_steps:
        return {'name': name, 'detected': True, 'direction': 'bearish',
                'reference_accuracy': REFERENCE_ACCURACY[name],
                'note': f'陰線が{min_steps}本連続で寄り付きが切り下がる段違い下落'}
    return _empty(name)


def detect_n_pattern(df, lookback=30, order=3):
    """
    N字: 安値→高値→浅い押し目（安値を切り上げ）→直近高値を上抜け、というN字型の
    買い継続パターン。押し目からの直近ブレイクを検出条件とする。
    """
    name = 'N字'
    if df is None or len(df) < lookback:
        return _empty(name, 'データ不足')

    recent = df.tail(lookback)
    peaks, troughs = _find_local_extrema(recent['Close'], order=order)
    if len(peaks) < 1 or len(troughs) < 2:
        return _empty(name)

    # 直近の並び: trough(安値1) -> peak(高値) -> trough(押し目安値2) の順であること
    last_trough2 = troughs[-1]
    prior_peaks = [p for p in peaks if p < last_trough2]
    if not prior_peaks:
        return _empty(name)
    last_peak = prior_peaks[-1]
    prior_troughs = [t for t in troughs if t < last_peak]
    if not prior_troughs:
        return _empty(name)
    first_trough = prior_troughs[-1]

    low1 = recent['Close'].iloc[first_trough]
    peak_val = recent['Close'].iloc[last_peak]
    low2 = recent['Close'].iloc[last_trough2]
    last_close = recent['Close'].iloc[-1]

    higher_low = low2 > low1
    breakout = last_close > peak_val

    if higher_low and breakout:
        return {'name': name, 'detected': True, 'direction': 'bullish',
                'reference_accuracy': REFERENCE_ACCURACY[name],
                'note': f'押し目の切り上げ（{low1:.0f}→{low2:.0f}）後、直近高値{peak_val:.0f}を上抜け'}
    if higher_low:
        return _empty(name, f'押し目は切り上げているが直近高値{peak_val:.0f}のブレイク待ち')
    return _empty(name)


def detect_v_pattern(df, lookback=15, drop_threshold=0.08, recover_ratio=0.6):
    """
    V字: 直近lookback日以内で高値から一定以上（drop_threshold）急落した後、
    その下落幅の一定割合（recover_ratio）以上を短期間で急速に取り戻している場合に検出。
    """
    name = 'V字'
    if df is None or len(df) < lookback:
        return _empty(name, 'データ不足')

    recent = df.tail(lookback)
    peak_idx = recent['Close'].values[:len(recent) - 3].argmax() if len(recent) > 3 else 0
    peak_val = recent['Close'].iloc[peak_idx]
    after_peak = recent['Close'].iloc[peak_idx:]
    if len(after_peak) < 4:
        return _empty(name)

    trough_pos_in_after = after_peak.values.argmin()
    trough_val = after_peak.iloc[trough_pos_in_after]
    last_close = recent['Close'].iloc[-1]

    if peak_val <= 0 or trough_pos_in_after == 0:
        return _empty(name)

    drop = (peak_val - trough_val) / peak_val
    if drop < drop_threshold:
        return _empty(name)

    recovered = (last_close - trough_val) / (peak_val - trough_val) if peak_val != trough_val else 0
    detected = recovered >= recover_ratio and last_close > trough_val

    note = f'高値{peak_val:.0f}→安値{trough_val:.0f}（{drop*100:.1f}%下落）から' \
           f'{recovered*100:.0f}%戻し' + ('（V字回復を検出）' if detected else '（戻りが不十分）')
    return {'name': name, 'detected': detected, 'direction': 'bullish' if detected else None,
            'reference_accuracy': REFERENCE_ACCURACY[name], 'note': note}


ALL_DETECTORS = [
    detect_sanzan, detect_sankawa, detect_sanpei, detect_sanku, detect_sanpou,
    detect_tasuki, detect_kenuki, detect_danchigai, detect_n_pattern, detect_v_pattern,
]


def detect_all_patterns(df):
    """全10パターンを検出して list[dict] で返す。"""
    results = []
    for fn in ALL_DETECTORS:
        try:
            results.append(fn(df))
        except Exception as e:
            results.append({'name': fn.__name__, 'detected': False, 'direction': None,
                             'reference_accuracy': None, 'note': f'検出エラー: {e}'})
    return results


def sakata_score(pattern_results):
    """
    検出されたパターンから0〜1のスコア（README仕様の「酒田五法：25%」に対応する係数）を算出する。
    検出された bullish パターンのうち最も参考精度が高いものを採用し、
    bearish パターンが検出されている場合は減点する。
    パターンが1つも検出されなかった場合は None（判定不能ではなく「該当なし」= 0点）を返す。
    """
    bullish = [p for p in pattern_results if p['detected'] and p['direction'] == 'bullish']
    bearish = [p for p in pattern_results if p['detected'] and p['direction'] == 'bearish']

    if not bullish and not bearish:
        return 0.0, []

    score = 0.0
    reasons = []
    if bullish:
        best = max(bullish, key=lambda p: p['reference_accuracy'] or 0)
        score += (best['reference_accuracy'] or 0) / 100
        reasons.append(f"{best['name']}（買いシグナル・参考精度{best['reference_accuracy']}%）：{best['note']}")
    if bearish:
        worst = max(bearish, key=lambda p: p['reference_accuracy'] or 0)
        score -= (worst['reference_accuracy'] or 0) / 100 * 0.5
        reasons.append(f"{worst['name']}（売りシグナル・参考精度{worst['reference_accuracy']}%）：{worst['note']}")

    return max(0.0, min(score, 1.0)), reasons
