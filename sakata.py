#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sakata.py — 酒田五法 10パターンの検出モジュール

日足OHLCVデータ（pandas DataFrame、Open/High/Low/Close列、古い順）から10パターンを検出する。

【2026-09-16 全面改訂：教科書どおりの定義への書き直し】
旧版（〜2026-09-15）には次の問題があり、J-Quants全銘柄×約2年の実測で「ほぼ毎日どこかで
検出される＝シグナルとして機能していない」ことが確認されたため、全パターンを書き直した。
  - 三山・三川：谷（山）を高値（安値）系列から探しており、別系列の極値を見ていた。
    三尊（中央の山が最も高い形）を取りこぼしていた。
  - ネックライン割れ・高値ブレイク・V字戻しが「一度起きたら何週間でも検出され続ける」
    （鮮度判定なし）。N字は全銘柄日の約17%、毛抜きは約40%で検出されていた。
  - たすき線：窓を開けた陰線が前日の足より丸ごと上にある形を検出しており、定義と別物だった。
  - 三兵・毛抜き・三法：出現位置（底値圏／天井圏／トレンド中）と実体の大きさの条件が無かった。
  - 三空：4本連続での3連続窓しか拾えず、ほぼ出現しなかった。

【全パターン共通の設計】
1. 「完成した日」だけ検出する（鮮度）。ネックライン割れ・高値ブレイク・半値戻し等は
   「直近の足で初めてラインを越えた」ときだけ True。翌日以降は検出しない。
   日次スクリーニングで毎朝「今日新しく点灯したシグナル」を拾うための仕様。
2. 出現位置の条件を持つ。底打ち系（三川・赤三兵・毛抜き底・三空叩き込み）は下落局面、
   天井系（三山・黒三兵・毛抜き天井・三空踏み上げ）は上昇局面でのみ検出する。
   継続系（三法・たすき・段違い・N字）はトレンド方向と一致するときのみ検出する。
3. 値幅はATR（14日平均の真の値幅）で正規化する。「大陽線」「意味のある山と谷」を
   銘柄ごとの普段の値動きに対して判定し、株価水準やボラティリティの違いに左右されないようにする。

【reference_accuracy について】
REFERENCE_ACCURACY は evaluate_sakata.py で実測した「検出翌日始値→20営業日後終値」の
方向一致率（買い＝上昇、売り＝下落した割合、%）。J-Quantsキャッシュ（2024-06〜2026-06、
流動性あり銘柄）での値で、将来の成績を保証するものではない。更新手順は
evaluate_sakata.py の冒頭を参照。

各検出関数は以下の形式の dict を返す（旧版と互換）:
  {
    'name': '三山', 'detected': bool, 'direction': 'bullish' | 'bearish' | None,
    'reference_accuracy': 52,  # 実測の方向一致率(%)。未計測はNone
    'note': '...',             # 検出理由の簡単な説明（Noneの場合あり）
  }
"""

import numpy as np
import pandas as pd


# evaluate_sakata.py の実測値。update_sakata_accuracy.py が測定結果JSONから自動で書き換える（手で編集しない）。
# キー：(パターン名, 方向)。値：取引可能な銘柄で、検出翌日の始値→20営業日後の終値が「当たる向き」
# （買い＝上昇、売り＝下落）に動いた割合（%）。未計測は None。
# --- REFERENCE_ACCURACY_BEGIN ---
REFERENCE_ACCURACY = {
    ('N字', 'bullish'): 53,
    ('V字', 'bullish'): 55,
    ('たすき線', 'bearish'): 46,
    ('たすき線', 'bullish'): 55,
    ('三兵', 'bearish'): 44,
    ('三兵', 'bullish'): 49,
    ('三山', 'bearish'): 42,
    ('三川', 'bullish'): 50,
    ('三法', 'bullish'): 49,
    ('三空', 'bearish'): 44,
    ('三空', 'bullish'): 69,
    ('段違い', 'bearish'): 43,
    ('段違い', 'bullish'): 50,
    ('毛抜き（かい値）', 'bearish'): 45,
    ('毛抜き（かい値）', 'bullish'): 52,
}
REFERENCE_ACCURACY_SOURCE = {'file': 'data/backtest_out/sakata_eval_final.json', 'data_fingerprint': '5032c4574dd07ce7', 'code_commit': '082a0e6b', 'min_count': 300, 'metric': '取引可能銘柄・検出翌日始値→20営業日後終値の方向一致率(%)'}
# --- REFERENCE_ACCURACY_END ---


ATR_PERIOD = 14
TREND_MA = 25


# ---------------------------------------------------------------------------
# 共通ヘルパー
# ---------------------------------------------------------------------------

def _empty(name, note=None):
    return {'name': name, 'detected': False, 'direction': None, 'reference_accuracy': None, 'note': note}


def _hit(name, direction, note):
    return {'name': name, 'detected': True, 'direction': direction,
            'reference_accuracy': REFERENCE_ACCURACY.get((name, direction)), 'note': note}


class _Bars:
    """
    OHLCの配列だけを持つ軽量な入れ物。detect_all_patterns() が DataFrame を1回だけ配列化して
    各検出関数に渡すために使う（pandasの列アクセスを10パターン×毎日繰り返すと遅いため）。
    検出関数は DataFrame と _Bars のどちらを受け取っても同じ結果を返す。
    """

    __slots__ = ('o', 'h', 'l', 'c')

    def __init__(self, o, h, l, c):
        self.o, self.h, self.l, self.c = o, h, l, c

    @classmethod
    def from_df(cls, df):
        return cls(df['Open'].to_numpy(dtype=float), df['High'].to_numpy(dtype=float),
                   df['Low'].to_numpy(dtype=float), df['Close'].to_numpy(dtype=float))

    def __len__(self):
        return len(self.c)

    def tail(self, n):
        return _Bars(self.o[-n:], self.h[-n:], self.l[-n:], self.c[-n:])


def _ohlc(df):
    if isinstance(df, _Bars):
        return df.o, df.h, df.l, df.c
    return (df['Open'].to_numpy(dtype=float), df['High'].to_numpy(dtype=float),
            df['Low'].to_numpy(dtype=float), df['Close'].to_numpy(dtype=float))


def _atr_at(h, l, c, idx, period=ATR_PERIOD):
    """idx本目（位置インデックス）時点のATR（真の値幅の単純平均）。計算できなければNone。"""
    if idx < period:
        return None
    hi = h[idx - period + 1:idx + 1]
    lo = l[idx - period + 1:idx + 1]
    prev_c = c[idx - period:idx]
    tr = np.maximum(hi - lo, np.maximum(np.abs(hi - prev_c), np.abs(lo - prev_c)))
    val = float(tr.mean())
    return val if val > 0 and np.isfinite(val) else None


def _sma_at(c, idx, window=TREND_MA):
    if idx < window - 1:
        return None
    return float(c[idx - window + 1:idx + 1].mean())


def _find_local_extrema(series, order=3):
    """
    近傍比較による極大・極小の検出（scipyに依存しないための自前実装）。
    series: pandas Series または 1次元配列。order: 前後何本と比較するか。
    戻り値: (peak_idx_list, trough_idx_list) — 位置インデックス（0始まり）
    同値が並ぶ場合は最初の足だけを極値とする。
    """
    values = series.values if hasattr(series, 'values') else np.asarray(series, dtype=float)
    n = len(values)
    if n < 2 * order + 1:
        return [], []
    windows = np.lib.stride_tricks.sliding_window_view(values, 2 * order + 1)
    # argmax/argmin は最初に現れた位置を返すので「中心が最大（最小）かつ、それより前に同値が無い」と同じ判定
    peaks = (np.flatnonzero(windows.argmax(axis=1) == order) + order).tolist()
    troughs = (np.flatnonzero(windows.argmin(axis=1) == order) + order).tolist()
    return peaks, troughs


def _body(o, c, i):
    return abs(c[i] - o[i])


def _is_bull(o, c, i):
    return c[i] > o[i]


def _is_bear(o, c, i):
    return c[i] < o[i]


def _line_value(x1, y1, x2, y2, x):
    if x2 == x1:
        return y2
    return y1 + (y2 - y1) * (x - x1) / (x2 - x1)


# ---------------------------------------------------------------------------
# 1. 三山（三尊を含む）／2. 三川（逆三尊を含む）
# ---------------------------------------------------------------------------

def _triple_formation(df, kind, window=100, order=3, peak_tolerance=0.06,
                      min_swing_atr=1.0, max_bars_after_last=30):
    """
    三山（kind='top'）／三川（kind='bottom'）の共通実装。

    三山の定義:
      - 直近window本の中に高値の山が3つ（P1<P2<P3、高値系列の極大）
      - 3つの山の高値が peak_tolerance 以内に収まる。三尊（中央が最も高い）も含む
      - 山と山の間の谷（安値系列の最安値 T1, T2）が、隣の山より min_swing_atr×ATR 以上低い
      - 天井圏であること：P1より前20本の最安値が、2つの谷のどちらよりも低い（上昇してきた）
      - ネックライン＝T1とT2を結んだ直線。直近の足の終値が初めてネックラインを割り込んだ日に検出
        （前日終値はネックライン以上、かつP3以降でまだ一度も割っていない）
      - P3からブレイクまで max_bars_after_last 本以内
    三川はこの上下反転。
    """
    top = kind == 'top'
    name = '三山' if top else '三川'
    if df is None or len(df) < 40:
        return _empty(name, 'データ不足')

    recent = df.tail(window)
    o, h, l, c = _ohlc(recent)
    n = len(recent)
    last = n - 1
    atr = _atr_at(h, l, c, last)
    if atr is None:
        return _empty(name, 'データ不足')

    extreme_series = h if top else l
    peaks, troughs = _find_local_extrema(extreme_series, order=order)
    pivots = peaks if top else troughs
    if len(pivots) < 3:
        return _empty(name)

    p1, p2, p3 = pivots[-3], pivots[-2], pivots[-1]
    if p3 >= last or last - p3 > max_bars_after_last:
        return _empty(name)
    if p2 - p1 < 3 or p3 - p2 < 3:
        return _empty(name)

    v1, v2, v3 = extreme_series[p1], extreme_series[p2], extreme_series[p3]
    hi_v, lo_v = max(v1, v2, v3), min(v1, v2, v3)
    if hi_v <= 0 or (hi_v - lo_v) / hi_v > peak_tolerance:
        return _empty(name)
    # 3つ目の山（谷）の後に、3つの山を明確に上回る高値（谷を下回る安値）が出ていれば形が崩れている
    if top and h[p3 + 1:last + 1].max() > hi_v * (1 + peak_tolerance / 2):
        return _empty(name)
    if (not top) and l[p3 + 1:last + 1].min() < lo_v * (1 - peak_tolerance / 2):
        return _empty(name)

    # 山と山の間の谷（三川なら谷と谷の間の山）は、反対側の系列から取る
    between = l if top else h
    seg1 = between[p1 + 1:p2]
    seg2 = between[p2 + 1:p3]
    if len(seg1) == 0 or len(seg2) == 0:
        return _empty(name)
    if top:
        t1 = p1 + 1 + int(np.argmin(seg1))
        t2 = p2 + 1 + int(np.argmin(seg2))
    else:
        t1 = p1 + 1 + int(np.argmax(seg1))
        t2 = p2 + 1 + int(np.argmax(seg2))
    tv1, tv2 = between[t1], between[t2]

    # 意味のある山・谷か（ATR基準）
    if top:
        swings = [v1 - tv1, v2 - tv1, v2 - tv2, v3 - tv2]
    else:
        swings = [tv1 - v1, tv1 - v2, tv2 - v2, tv2 - v3]
    if min(swings) < min_swing_atr * atr:
        return _empty(name)

    # 出現位置：三山は上昇後の天井圏、三川は下落後の底値圏
    pre = between[max(0, p1 - 20):p1]
    if len(pre) < 5:
        return _empty(name)
    if top and not (pre.min() < min(tv1, tv2)):
        return _empty(name, '上昇後の天井圏ではない')
    if (not top) and not (pre.max() > max(tv1, tv2)):
        return _empty(name, '下落後の底値圏ではない')

    neck_now = _line_value(t1, tv1, t2, tv2, last)
    neck_prev = _line_value(t1, tv1, t2, tv2, last - 1)

    if top:
        already = any(c[i] < _line_value(t1, tv1, t2, tv2, i) for i in range(p3 + 1, last))
        fresh_break = c[last] < neck_now and c[last - 1] >= neck_prev and not already
    else:
        already = any(c[i] > _line_value(t1, tv1, t2, tv2, i) for i in range(p3 + 1, last))
        fresh_break = c[last] > neck_now and c[last - 1] <= neck_prev and not already

    shape = ''
    if v2 == hi_v and top and v2 > max(v1, v3):
        shape = '（三尊型）'
    if v2 == lo_v and (not top) and v2 < min(v1, v3):
        shape = '（逆三尊型）'

    if not fresh_break:
        state = 'ネックライン割れ待ち' if top else 'ネックライン突破待ち'
        return _empty(name, f'{name}{shape}の形は完成、{state}（ネックライン{neck_now:.0f}）')

    if top:
        note = f'三山{shape}：高値{v1:.0f}/{v2:.0f}/{v3:.0f}の3つの山の後、ネックライン{neck_now:.0f}を本日割り込み'
        return _hit(name, 'bearish', note)
    note = f'三川{shape}：安値{v1:.0f}/{v2:.0f}/{v3:.0f}の3つの谷の後、ネックライン{neck_now:.0f}を本日上抜け'
    return _hit(name, 'bullish', note)


def detect_sanzan(df, tolerance=0.06):
    """三山（三尊天井を含む）：天井圏の3つの山の後、ネックラインを初めて割り込んだ日に売りシグナル。"""
    return _triple_formation(df, 'top', peak_tolerance=tolerance)


def detect_sankawa(df, tolerance=0.06):
    """三川（逆三尊を含む）：底値圏の3つの谷の後、ネックラインを初めて上抜けた日に買いシグナル。"""
    return _triple_formation(df, 'bottom', peak_tolerance=tolerance)


# ---------------------------------------------------------------------------
# 3. 三兵（赤三兵／黒三兵＝三羽烏）
# ---------------------------------------------------------------------------

def detect_sanpei(df, min_body_atr=0.3, min_body_ratio=0.4):
    """
    赤三兵（買い）:
      - 直近3本がすべて陽線で、終値が1本ごとに切り上がる
      - 2本目・3本目の始値は前の足の実体内（前日始値〜前日終値）
      - 各足の実体が ATR×min_body_atr 以上、かつ値幅の min_body_ratio 以上（ヒゲばかりの小さな足は除外）
      - 3本目の実体が2本目の半分未満なら「先詰まり」として検出しない
      - 出現位置：1本目の前日終値が25日移動平均以下（底値圏・下落局面からの立ち上がり）
      - 4本前も同条件の陽線なら「4本目以降の継続」とみなし検出しない（完成日のみ）
    黒三兵（三羽烏、売り）はこの上下反転（出現位置は25日移動平均以上＝高値圏）。
    """
    name = '三兵'
    if df is None or len(df) < TREND_MA + 5:
        return _empty(name, 'データ不足')
    o, h, l, c = _ohlc(df)
    n = len(df)
    idx = [n - 3, n - 2, n - 1]
    atr = _atr_at(h, l, c, n - 4)
    sma_before = _sma_at(c, n - 4)
    if atr is None or sma_before is None:
        return _empty(name, 'データ不足')

    def solid(i):
        rng = h[i] - l[i]
        return rng > 0 and _body(o, c, i) >= min_body_atr * atr and _body(o, c, i) >= min_body_ratio * rng

    def chain(bull):
        for k, i in enumerate(idx):
            if bull and not _is_bull(o, c, i):
                return False
            if (not bull) and not _is_bear(o, c, i):
                return False
            if not solid(i):
                return False
            if k > 0:
                j = idx[k - 1]
                lo_b, hi_b = min(o[j], c[j]), max(o[j], c[j])
                if not (lo_b <= o[i] <= hi_b):
                    return False
                if bull and not c[i] > c[j]:
                    return False
                if (not bull) and not c[i] < c[j]:
                    return False
        return True

    for bull in (True, False):
        if not chain(bull):
            continue
        if _body(o, c, idx[2]) < 0.5 * _body(o, c, idx[1]):
            label = '赤三兵の先詰まり' if bull else '黒三兵の下げ渋り'
            return _empty(name, f'{label}（3本目の実体が小さい）')
        prev = n - 4
        prev_same = (_is_bull(o, c, prev) if bull else _is_bear(o, c, prev)) and solid(prev)
        if prev_same:
            return _empty(name, '4本以上の連続で三兵の完成日ではない')
        if bull and c[prev] <= sma_before:
            return _hit(name, 'bullish', '赤三兵：25日線以下の水準から、実体の大きい陽線が3本連続で切り上げ')
        if (not bull) and c[prev] >= sma_before:
            return _hit(name, 'bearish', '黒三兵（三羽烏）：25日線以上の水準から、実体の大きい陰線が3本連続で切り下げ')
        return _empty(name, '形は三兵だが出現位置が底値圏／高値圏ではない')
    return _empty(name)


# ---------------------------------------------------------------------------
# 4. 三空（三空踏み上げ／三空叩き込み）
# ---------------------------------------------------------------------------

def detect_sanku(df, window=15):
    """
    三空踏み上げ（売り）:
      - 直近の足で上に窓（当日安値＞前日高値）を開けた
      - その窓が、直近window本の上昇の中で3つ目の上窓（途中に下窓が無い）
      - 窓を開けた足同士の間に、それまでの高値を割り込むような押し（直近の窓を埋める下落）が無い
    三空叩き込み（買い）はこの上下反転。3つ目の窓で「行き過ぎ」と見て逆張りする古典的な解釈。
    4つ目以降の窓の日は検出しない（3つ目の完成日のみ）。
    """
    name = '三空'
    if df is None or len(df) < window + 1:
        return _empty(name, 'データ不足')
    o, h, l, c = _ohlc(df.tail(window + 1))
    n = len(o)
    last = n - 1

    gap_up = [i for i in range(1, n) if l[i] > h[i - 1]]
    gap_dn = [i for i in range(1, n) if h[i] < l[i - 1]]

    if gap_up and gap_up[-1] == last:
        seq = gap_up
        # 直近の下窓より後の上窓だけを数える
        if gap_dn:
            seq = [i for i in gap_up if i > gap_dn[-1]]
        if len(seq) == 3:
            first = seq[0]
            # 途中で最初の窓を埋める（窓の下限＝窓直前の高値を割る）押しがあれば無効
            floor = h[first - 1]
            if l[first:last + 1].min() > floor:
                return _hit(name, 'bearish', '三空踏み上げ：上昇の中で3つ目の窓を開けた（買われすぎで反落警戒）')
    if gap_dn and gap_dn[-1] == last:
        seq = gap_dn
        if gap_up:
            seq = [i for i in gap_dn if i > gap_up[-1]]
        if len(seq) == 3:
            first = seq[0]
            ceil = l[first - 1]
            if h[first:last + 1].max() < ceil:
                return _hit(name, 'bullish', '三空叩き込み：下落の中で3つ目の窓を開けた（売られすぎで反発期待）')
    return _empty(name)


# ---------------------------------------------------------------------------
# 5. 三法（上げ三法／下げ三法）
# ---------------------------------------------------------------------------

def detect_sanpou(df, min_lead_body_atr=1.0, max_middle_body_ratio=0.5, min_break_body_atr=0.6):
    """
    上げ三法（買い・トレンド継続）:
      - 起点：25日移動平均より上で出た大陽線（実体がATR×min_lead_body_atr以上、かつ値幅の60%以上）
      - その後2〜4本の小さな足（実体が大陽線の実体×max_middle_body_ratio以下）が、
        すべて大陽線の高値〜安値の範囲内に収まる（保ち合い・休み）
      - 直近の足が陽線（実体ATR×min_break_body_atr以上）で、大陽線の高値を終値で上抜けた日に検出
    下げ三法（売り）はこの上下反転。
    """
    name = '三法'
    if df is None or len(df) < TREND_MA + 8:
        return _empty(name, 'データ不足')
    o, h, l, c = _ohlc(df)
    n = len(df)
    last = n - 1

    for k in (3, 2, 4):  # 教科書の3本を優先し、2本・4本も許容
        lead = last - k - 1
        if lead < TREND_MA:
            continue
        atr = _atr_at(h, l, c, lead - 1)
        sma = _sma_at(c, lead)
        if atr is None or sma is None:
            continue
        rng = h[lead] - l[lead]
        lead_body = _body(o, c, lead)
        if rng <= 0 or lead_body < min_lead_body_atr * atr or lead_body < 0.6 * rng:
            continue
        mids = range(lead + 1, last)
        contained = all(h[i] <= h[lead] and l[i] >= l[lead] for i in mids)
        small = all(_body(o, c, i) <= max_middle_body_ratio * lead_body for i in mids)
        if not (contained and small):
            continue
        break_body = _body(o, c, last)
        if break_body < min_break_body_atr * atr:
            continue

        if _is_bull(o, c, lead) and c[lead] > sma and _is_bull(o, c, last) and c[last] > h[lead]:
            return _hit(name, 'bullish', f'上げ三法：大陽線の後{k}本の保ち合いを経て、陽線で大陽線の高値を上抜け')
        if _is_bear(o, c, lead) and c[lead] < sma and _is_bear(o, c, last) and c[last] < l[lead]:
            return _hit(name, 'bearish', f'下げ三法：大陰線の後{k}本の保ち合いを経て、陰線で大陰線の安値を下抜け')
    return _empty(name)


# ---------------------------------------------------------------------------
# 6. たすき線（上放れたすき／下放れたすき）
# ---------------------------------------------------------------------------

def detect_tasuki(df):
    """
    上放れたすき線（買い・トレンド継続）:
      - 25日移動平均より上（上昇局面）
      - 2本前→1本前：1本前の陽線が、2本前の高値より上に窓を開けて出る（1本前の安値＞2本前の高値）
      - 直近の足：陰線で、始値が1本前の陽線の実体内、終値が1本前の始値を下回る（窓に食い込む）が、
        2本前の高値は割らない（窓を埋めきらない）
    下放れたすき線（売り）はこの上下反転。

    ※旧版READMEでは「転換シグナル」と記載していたが、酒田五法の「上放れ（下放れ）たすき」は
      窓を埋めきらずにトレンドが続くことを示す継続パターンとして実装している。
    """
    name = 'たすき線'
    if df is None or len(df) < TREND_MA + 3:
        return _empty(name, 'データ不足')
    o, h, l, c = _ohlc(df)
    n = len(df)
    a, b, x = n - 3, n - 2, n - 1
    sma = _sma_at(c, b)
    if sma is None:
        return _empty(name, 'データ不足')

    if (c[b] > sma and _is_bull(o, c, b) and l[b] > h[a]
            and _is_bear(o, c, x) and o[b] <= o[x] <= c[b]
            and c[x] < o[b] and c[x] > h[a]):
        return _hit(name, 'bullish', '上放れたすき線：窓を開けた陽線の翌日の陰線が窓に食い込むも埋めきらず（上昇継続）')

    if (c[b] < sma and _is_bear(o, c, b) and h[b] < l[a]
            and _is_bull(o, c, x) and c[b] <= o[x] <= o[b]
            and c[x] > o[b] and c[x] < l[a]):
        return _hit(name, 'bearish', '下放れたすき線：窓を開けた陰線の翌日の陽線が窓に食い込むも埋めきらず（下落継続）')
    return _empty(name)


# ---------------------------------------------------------------------------
# 7. 毛抜き（毛抜き底／毛抜き天井）
# ---------------------------------------------------------------------------

def detect_kenuki(df, tolerance_atr=0.1, min_tolerance_pct=0.002, lookback=10, min_decline_atr=2.0):
    """
    毛抜き底（買い）:
      - 1本前が陰線、直近が陽線
      - 2本の安値がほぼ同じ（差がATR×tolerance_atr以内、ただし最低でも株価の0.2%は許容）
      - その安値が直近lookback本の最安値（下落の底で出ている）
      - 下落局面：1本前の終値が25日移動平均以下、かつlookback本前の高値から安値までATR×min_decline_atr以上下げている
      - 直近の陽線の終値が1本前の陰線の終値を上回る（下げ止まりの確認）
    毛抜き天井（売り）はこの上下反転。
    """
    name = '毛抜き（かい値）'
    if df is None or len(df) < TREND_MA + lookback:
        return _empty(name, 'データ不足')
    o, h, l, c = _ohlc(df)
    n = len(df)
    a, b = n - 2, n - 1
    atr = _atr_at(h, l, c, a)
    sma = _sma_at(c, a)
    if atr is None or sma is None:
        return _empty(name, 'データ不足')
    start = max(0, b - lookback + 1)

    tol_low = max(tolerance_atr * atr, l[a] * min_tolerance_pct)
    if (_is_bear(o, c, a) and _is_bull(o, c, b) and abs(l[a] - l[b]) <= tol_low
            and min(l[a], l[b]) <= l[start:b + 1].min() + 1e-9
            and c[a] <= sma and h[start:b + 1].max() - min(l[a], l[b]) >= min_decline_atr * atr
            and c[b] > c[a]):
        return _hit(name, 'bullish',
                    f'毛抜き底：下落の底で安値がほぼ同水準（{l[a]:.0f}/{l[b]:.0f}）に揃い、陽線で反発')

    tol_high = max(tolerance_atr * atr, h[a] * min_tolerance_pct)
    if (_is_bull(o, c, a) and _is_bear(o, c, b) and abs(h[a] - h[b]) <= tol_high
            and max(h[a], h[b]) >= h[start:b + 1].max() - 1e-9
            and c[a] >= sma and max(h[a], h[b]) - l[start:b + 1].min() >= min_decline_atr * atr
            and c[b] < c[a]):
        return _hit(name, 'bearish',
                    f'毛抜き天井：上昇の天井で高値がほぼ同水準（{h[a]:.0f}/{h[b]:.0f}）に揃い、陰線で反落')
    return _empty(name)


# ---------------------------------------------------------------------------
# 8. 段違い
# ---------------------------------------------------------------------------

def detect_danchigai(df, min_steps=3, min_gap_atr=0.1):
    """
    段違い（階段状の上昇／下落）※酒田五法の古典的な名称ではなく、引き継ぎ資料由来の独自パターン:
      - 直近min_steps本がすべて陽線で、各足の始値が前日終値よりATR×min_gap_atr以上高く始まる
        （寄り付きが階段状にずれて上がっていく、窓未満の強い買い）
      - 25日移動平均より上（上昇トレンド中）
      - その1本前は条件を満たさない（min_steps本目の完成日のみ検出）
    下落版はこの上下反転。
    """
    name = '段違い'
    if df is None or len(df) < TREND_MA + min_steps + 2:
        return _empty(name, 'データ不足')
    o, h, l, c = _ohlc(df)
    n = len(df)
    atr = _atr_at(h, l, c, n - min_steps - 1)
    sma = _sma_at(c, n - 1)
    if atr is None or sma is None:
        return _empty(name, 'データ不足')
    gap = min_gap_atr * atr

    def step_up(i):
        return _is_bull(o, c, i) and o[i] >= c[i - 1] + gap

    def step_dn(i):
        return _is_bear(o, c, i) and o[i] <= c[i - 1] - gap

    steps = range(n - min_steps, n)
    first_prev = n - min_steps - 1
    if all(step_up(i) for i in steps) and not step_up(first_prev) and c[-1] > sma:
        return _hit(name, 'bullish', f'段違い上昇：陽線が{min_steps}本連続で前日終値より高く寄り付き')
    if all(step_dn(i) for i in steps) and not step_dn(first_prev) and c[-1] < sma:
        return _hit(name, 'bearish', f'段違い下落：陰線が{min_steps}本連続で前日終値より安く寄り付き')
    return _empty(name)


# ---------------------------------------------------------------------------
# 9. N字
# ---------------------------------------------------------------------------

def detect_n_pattern(df, lookback=60, order=3, min_leg_atr=2.0, min_retrace=0.236, max_retrace=0.786):
    """
    N字（買い・上昇継続）:
      - 終値の極値で 安値L1 → 高値H → 押し安値L2 の順に並ぶ（L2＞L1＝安値切り上げ）
      - 上げ幅（H−L1）がATR×min_leg_atr以上の意味のある上昇
      - 押しの深さ（H−L2）/（H−L1）が min_retrace〜max_retrace（浅すぎ・深すぎを除外）
      - 直近の終値が初めて高値Hを上抜けた日に検出（前日終値はH以下、L2以降で一度も超えていない）
    """
    name = 'N字'
    if df is None or len(df) < lookback:
        return _empty(name, 'データ不足')
    recent = df.tail(lookback)
    o, h, l, c = _ohlc(recent)
    n = len(recent)
    last = n - 1
    atr = _atr_at(h, l, c, last)
    if atr is None:
        return _empty(name, 'データ不足')

    peaks, troughs = _find_local_extrema(c, order=order)
    if not peaks or len(troughs) < 2:
        return _empty(name)
    l2 = troughs[-1]
    prior_peaks = [p for p in peaks if p < l2]
    if not prior_peaks:
        return _empty(name)
    hp = prior_peaks[-1]
    prior_troughs = [t for t in troughs if t < hp]
    if not prior_troughs:
        return _empty(name)
    l1 = prior_troughs[-1]

    low1, peak_val, low2 = c[l1], c[hp], c[l2]
    leg = peak_val - low1
    if leg < min_leg_atr * atr or low2 <= low1:
        return _empty(name)
    retrace = (peak_val - low2) / leg
    if not (min_retrace <= retrace <= max_retrace):
        return _empty(name)
    if c[l2 + 1:last].size and c[l2 + 1:last].max() > peak_val:
        return _empty(name)  # 既にブレイク済み（本日が初回ではない）

    if c[last] > peak_val and c[last - 1] <= peak_val:
        return _hit(name, 'bullish',
                    f'N字：{low1:.0f}→{peak_val:.0f}の上昇後、{retrace * 100:.0f}%押し（{low2:.0f}）から高値{peak_val:.0f}を本日上抜け')
    return _empty(name, f'押し目は切り上げているが直近高値{peak_val:.0f}のブレイク待ち')


# ---------------------------------------------------------------------------
# 10. V字
# ---------------------------------------------------------------------------

def detect_v_pattern(df, lookback=20, drop_threshold=0.08, min_drop_atr=3.0, max_fall_bars=10,
                     max_recover_bars=8, recover_ratio=0.5):
    """
    V字回復（買い）:
      - 直近lookback本の中の高値（終値）Pから、max_fall_bars本以内に安値（終値）Tまで急落
        （下落率drop_threshold以上、かつATR×min_drop_atr以上）
      - 安値Tは直近max_recover_bars本以内（底打ちが最近）
      - 直近の終値で下落幅の recover_ratio（既定：半値）を初めて取り戻した日に検出
    """
    name = 'V字'
    if df is None or len(df) < lookback + ATR_PERIOD + 1:
        return _empty(name, 'データ不足')
    o, h, l, c = _ohlc(df)
    n = len(df)
    last = n - 1
    start = n - lookback

    t_idx = start + int(np.argmin(c[start:last]))  # 安値は本日より前
    if last - t_idx > max_recover_bars or t_idx == start:
        return _empty(name)
    p_from = max(start, t_idx - max_fall_bars)
    p_idx = p_from + int(np.argmax(c[p_from:t_idx]))
    peak_val, trough_val = c[p_idx], c[t_idx]
    if peak_val <= 0 or peak_val <= trough_val:
        return _empty(name)
    drop = (peak_val - trough_val) / peak_val
    atr = _atr_at(h, l, c, p_idx) or _atr_at(h, l, c, t_idx)
    if atr is None or drop < drop_threshold or (peak_val - trough_val) < min_drop_atr * atr:
        return _empty(name)

    threshold = trough_val + (peak_val - trough_val) * recover_ratio
    recovered_now = (c[last] - trough_val) / (peak_val - trough_val)
    between = c[t_idx + 1:last]  # 安値の翌日〜前日（空なら本日が安値の翌日）
    first_cross = c[last] >= threshold and (between.size == 0 or between.max() < threshold)
    if first_cross:
        return _hit(name, 'bullish',
                    f'V字回復：{peak_val:.0f}→{trough_val:.0f}（{drop * 100:.1f}%急落）から{recovered_now * 100:.0f}%戻し')
    return _empty(name, f'急落後の戻り{recovered_now * 100:.0f}%（半値戻し待ち）')


# ---------------------------------------------------------------------------
# まとめ
# ---------------------------------------------------------------------------

ALL_DETECTORS = [
    detect_sanzan, detect_sankawa, detect_sanpei, detect_sanku, detect_sanpou,
    detect_tasuki, detect_kenuki, detect_danchigai, detect_n_pattern, detect_v_pattern,
]


def detect_all_patterns(df):
    """全10パターンを検出して list[dict] で返す。"""
    results = []
    bars = _Bars.from_df(df) if df is not None and not isinstance(df, _Bars) else df
    for fn in ALL_DETECTORS:
        try:
            results.append(fn(bars))
        except Exception as e:
            results.append({'name': fn.__name__, 'detected': False, 'direction': None,
                            'reference_accuracy': None, 'note': f'検出エラー: {e}'})
    return results


def sakata_score(pattern_results):
    """
    検出されたパターンから0〜1のスコア（README仕様の「酒田五法：25%」に対応する係数）を算出する。
    検出された bullish パターンのうち最も実測精度が高いものを採用し、
    bearish パターンが検出されている場合は減点する。
    パターンが1つも検出されなかった場合は 0.0（該当なし）を返す。
    実測精度が未計測（None）のパターンは50%（五分五分）として扱う。
    """
    bullish = [p for p in pattern_results if p['detected'] and p['direction'] == 'bullish']
    bearish = [p for p in pattern_results if p['detected'] and p['direction'] == 'bearish']

    if not bullish and not bearish:
        return 0.0, []

    def acc(p):
        v = p.get('reference_accuracy')
        return 50 if v is None else v

    score = 0.0
    reasons = []
    if bullish:
        best = max(bullish, key=acc)
        score += acc(best) / 100
        reasons.append(f"{best['name']}（買いシグナル・実測精度{_acc_label(best)}）：{best['note']}")
    if bearish:
        worst = max(bearish, key=acc)
        score -= acc(worst) / 100 * 0.5
        reasons.append(f"{worst['name']}（売りシグナル・実測精度{_acc_label(worst)}）：{worst['note']}")

    return max(0.0, min(score, 1.0)), reasons


def _acc_label(p):
    v = p.get('reference_accuracy')
    return '未計測' if v is None else f'{v}%'
