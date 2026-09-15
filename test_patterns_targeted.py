#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_patterns_targeted.py — 各酒田五法パターンを「教科書通りの形」で狙い撃ちして検出できるかを確認する。

test_synthetic.py はランダム生成データでの動作確認（壊れていないか）が目的だったが、
こちらは各detect_*関数が
  1) 意図した形が来た「完成日」に、確実にTrueを返すか
  2) 完成日の翌日（鮮度切れ）には検出しないか
  3) 出現位置が違う（底値圏でない赤三兵など）ときに検出しないか
を検証するためのより厳密なテスト。ロジックの取り違え（off-by-oneで絶対に発火しない、
逆に毎日発火する等）を防ぐ。

【2026-09-16】sakata.py の全面改訂（ATR正規化・出現位置・鮮度判定の導入）に合わせて書き直し。
"""

import numpy as np
import pandas as pd

import sakata as sk


def df_from_ohlc(rows):
    """rows: list of (open, high, low, close) タプル"""
    idx = pd.date_range('2025-01-01', periods=len(rows), freq='B')
    o, h, l, c = zip(*rows)
    return pd.DataFrame({'Open': o, 'High': h, 'Low': l, 'Close': c}, index=idx)


def bars_from_closes(closes, wick=0.004, first_open=None):
    """終値の列から、始値＝前日終値・上下に小さなヒゲを付けた足を作る。"""
    rows = []
    prev = closes[0] if first_open is None else first_open
    for c in closes:
        o = prev
        rows.append((o, max(o, c) * (1 + wick), min(o, c) * (1 - wick), c))
        prev = c
    return rows


def path(*points, steps=5):
    """(価格, 価格, ...) を各区間steps本の直線でつないだ終値列。"""
    out = [points[0]]
    for a, b in zip(points[:-1], points[1:]):
        out += list(np.linspace(a, b, steps + 1))[1:]
    return out


def mirror(rows, k=400.0):
    """上下反転（三山→三川などのテスト用）。"""
    return [(k - o, k - l, k - h, k - c) for (o, h, l, c) in rows]


def check(label, r, detected, direction=None):
    print(f'{label}:', r)
    assert r['detected'] == detected, f'{label}: detected={r["detected"]}（期待 {detected}） note={r["note"]}'
    if detected:
        assert r['direction'] == direction, f'{label}: direction={r["direction"]}（期待 {direction}）'


# ---------------------------------------------------------------- 三山／三川

def _sanzan_rows():
    closes = path(80, 100, steps=25)                 # 上昇してくる（天井圏の前提）
    closes += path(100, 120, 108, 124, 107, 120, steps=6)[1:]  # 山120 / 谷108 / 山124（三尊）/ 谷107 / 山120
    closes += [117, 114, 111, 109]                    # ネックライン（≒106〜107）の手前まで下げる
    return bars_from_closes(closes)


def test_sanzan_breaks_neckline_today():
    rows = _sanzan_rows() + bars_from_closes([101], first_open=109)
    check('sanzan', sk.detect_sanzan(df_from_ohlc(rows)), True, 'bearish')


def test_sanzan_not_detected_next_day():
    rows = _sanzan_rows() + bars_from_closes([101, 99], first_open=109)
    check('sanzan_stale', sk.detect_sanzan(df_from_ohlc(rows)), False)


def test_sanzan_requires_prior_uptrend():
    closes = path(160, 110, steps=25)                  # 上から下げてきた場合は天井圏ではない
    closes += path(110, 120, 108, 124, 107, 120, steps=6)[1:] + [117, 114, 111, 109]
    rows = bars_from_closes(closes) + bars_from_closes([101], first_open=109)
    check('sanzan_no_uptrend', sk.detect_sanzan(df_from_ohlc(rows)), False)


def test_sankawa_breaks_neckline_today():
    rows = mirror(_sanzan_rows() + bars_from_closes([101], first_open=109))
    check('sankawa', sk.detect_sankawa(df_from_ohlc(rows)), True, 'bullish')


def test_sankawa_not_detected_next_day():
    rows = mirror(_sanzan_rows() + bars_from_closes([101, 99], first_open=109))
    check('sankawa_stale', sk.detect_sankawa(df_from_ohlc(rows)), False)


# ---------------------------------------------------------------- 三兵

def _decline(n=30, start=120, end=100):
    return bars_from_closes(path(start, end, steps=n))


def test_akasanpei_bottom():
    rows = _decline() + [(99, 103.5, 98.5, 103), (101, 106.5, 100.5, 106), (104, 110, 103.5, 109.5)]
    check('akasanpei', sk.detect_sanpei(df_from_ohlc(rows)), True, 'bullish')


def test_kurosanpei_top():
    rows = mirror(_decline() + [(99, 103.5, 98.5, 103), (101, 106.5, 100.5, 106), (104, 110, 103.5, 109.5)])
    # mirror後：上昇してきた高値圏から陰線3本
    check('kurosanpei', sk.detect_sanpei(df_from_ohlc(rows)), True, 'bearish')


def test_akasanpei_sakizumari_not_detected():
    rows = _decline() + [(99, 103.5, 98.5, 103), (101, 106.5, 100.5, 106), (105, 107.5, 104.5, 106.8)]
    check('akasanpei_sakizumari', sk.detect_sanpei(df_from_ohlc(rows)), False)


def test_akasanpei_not_at_bottom_not_detected():
    rows = bars_from_closes(path(60, 100, steps=30)) + \
        [(99, 103.5, 98.5, 103), (101, 106.5, 100.5, 106), (104, 110, 103.5, 109.5)]
    # 直前まで大きく上げている＝25日線より上。ただし直前足が陽線なので「4本目以降」扱いにもなりうるため、
    # 直前足を陰線に差し替えて「出現位置」だけで弾かれることを確認する
    rows[-4] = (100.5, 101, 99, 99.2)
    check('akasanpei_not_bottom', sk.detect_sanpei(df_from_ohlc(rows)), False)


# ---------------------------------------------------------------- 三空

def _gap_down_rows(n_gaps):
    rows = bars_from_closes(path(130, 120, steps=20))
    for g in range(n_gaps):
        # 窓を開けて下げる足（高値＜前日安値）
        prev_low = rows[-1][2]
        o = prev_low - 1.5
        c = o - 2
        rows.append((o, o + 0.3, c - 0.3, c))
        if g < n_gaps - 1:
            rows.append((c, c + 0.8, c - 0.8, c - 0.2))  # 窓を埋めない小さな足
    return rows


def test_sanku_tatakikomi_third_gap():
    check('sanku_tatakikomi', sk.detect_sanku(df_from_ohlc(_gap_down_rows(3))), True, 'bullish')


def test_sanku_fourth_gap_not_detected():
    check('sanku_4th', sk.detect_sanku(df_from_ohlc(_gap_down_rows(4))), False)


def test_sanku_fumiage_third_gap():
    check('sanku_fumiage', sk.detect_sanku(df_from_ohlc(mirror(_gap_down_rows(3)))), True, 'bearish')


# ---------------------------------------------------------------- 三法

def _uptrend(n=30, start=90, end=110):
    return bars_from_closes(path(start, end, steps=n))


def test_agesanpou():
    rows = _uptrend() + [(110, 121, 109.5, 120), (119, 120, 115, 116), (116, 118, 113, 114),
                         (114, 117, 112, 116), (116, 125, 115.5, 124)]
    check('agesanpou', sk.detect_sanpou(df_from_ohlc(rows)), True, 'bullish')


def test_sagesanpou():
    rows = mirror(_uptrend() + [(110, 121, 109.5, 120), (119, 120, 115, 116), (116, 118, 113, 114),
                                (114, 117, 112, 116), (116, 125, 115.5, 124)])
    # mirror後：下落トレンド中の大陰線→保ち合い→安値割れ
    check('sagesanpou', sk.detect_sanpou(df_from_ohlc(rows)), True, 'bearish')


def test_sanpou_middle_breaks_range_not_detected():
    rows = _uptrend() + [(110, 121, 109.5, 120), (119, 120, 108, 116), (116, 118, 113, 114),
                         (114, 117, 112, 116), (116, 125, 115.5, 124)]
    check('sanpou_not_contained', sk.detect_sanpou(df_from_ohlc(rows)), False)


# ---------------------------------------------------------------- たすき線

def test_uwapanare_tasuki():
    rows = _uptrend() + [(109, 111, 108, 110.5), (112.5, 117.5, 112, 117), (114, 115, 111.2, 111.5)]
    check('uwapanare_tasuki', sk.detect_tasuki(df_from_ohlc(rows)), True, 'bullish')


def test_tasuki_gap_filled_not_detected():
    rows = _uptrend() + [(109, 111, 108, 110.5), (112.5, 117.5, 112, 117), (114, 115, 110, 110.6)]
    check('tasuki_filled', sk.detect_tasuki(df_from_ohlc(rows)), False)


def test_shitapanare_tasuki():
    rows = mirror(_uptrend() + [(109, 111, 108, 110.5), (112.5, 117.5, 112, 117), (114, 115, 111.2, 111.5)])
    check('shitapanare_tasuki', sk.detect_tasuki(df_from_ohlc(rows)), True, 'bearish')


# ---------------------------------------------------------------- 毛抜き

def test_kenuki_bottom():
    rows = _decline(n=40, start=130, end=100) + [(102, 102.5, 98, 99), (98.5, 102, 98.05, 101.5)]
    check('kenuki_bottom', sk.detect_kenuki(df_from_ohlc(rows)), True, 'bullish')


def test_kenuki_bottom_in_uptrend_not_detected():
    rows = _uptrend(n=40, start=70, end=100) + [(102, 102.5, 98, 99), (98.5, 102, 98.05, 101.5)]
    check('kenuki_uptrend', sk.detect_kenuki(df_from_ohlc(rows)), False)


def test_kenuki_top():
    rows = mirror(_decline(n=40, start=130, end=100) + [(102, 102.5, 98, 99), (98.5, 102, 98.05, 101.5)])
    check('kenuki_top', sk.detect_kenuki(df_from_ohlc(rows)), True, 'bearish')


# ---------------------------------------------------------------- 段違い

def test_danchigai_up():
    rows = _uptrend() + [(111, 113.3, 110.8, 113), (113.6, 115.8, 113.4, 115.5), (116.2, 118.3, 116, 118)]
    check('danchigai_up', sk.detect_danchigai(df_from_ohlc(rows)), True, 'bullish')


def test_danchigai_fourth_step_not_detected():
    rows = _uptrend() + [(111, 113.3, 110.8, 113), (113.6, 115.8, 113.4, 115.5), (116.2, 118.3, 116, 118),
                         (118.6, 120.3, 118.4, 120)]
    check('danchigai_4th', sk.detect_danchigai(df_from_ohlc(rows)), False)


# ---------------------------------------------------------------- N字

def _n_rows():
    closes = path(120, 100, steps=45)            # 助走の下げ（安値100で底）
    closes += path(100, 130, steps=8)[1:]        # 上昇（高値130）
    closes += path(130, 115, steps=5)[1:]        # 押し（安値115＝50%押し、安値切り上げ）
    closes += [118, 121, 124, 128]               # 高値130の手前まで戻す
    return closes


def test_n_pattern_breakout_today():
    rows = bars_from_closes(_n_rows() + [132])
    check('n_pattern', sk.detect_n_pattern(df_from_ohlc(rows)), True, 'bullish')


def test_n_pattern_not_detected_next_day():
    rows = bars_from_closes(_n_rows() + [132, 135])
    check('n_pattern_stale', sk.detect_n_pattern(df_from_ohlc(rows)), False)


def test_n_pattern_deep_pullback_not_detected():
    closes = path(120, 100, steps=45) + path(100, 130, steps=8)[1:] + path(130, 103, steps=5)[1:] + \
        [110, 118, 124, 128, 132]
    rows = bars_from_closes(closes)
    check('n_pattern_deep', sk.detect_n_pattern(df_from_ohlc(rows)), False)


# ---------------------------------------------------------------- V字

def _v_rows():
    closes = [1000.0] * 30 + path(1000, 880, steps=6)[1:] + [900, 930]
    return closes


def test_v_pattern_half_recovery_today():
    rows = bars_from_closes(_v_rows() + [945], wick=0.01)
    check('v_pattern', sk.detect_v_pattern(df_from_ohlc(rows)), True, 'bullish')


def test_v_pattern_not_detected_next_day():
    rows = bars_from_closes(_v_rows() + [945, 960], wick=0.01)
    check('v_pattern_stale', sk.detect_v_pattern(df_from_ohlc(rows)), False)


def test_v_pattern_shallow_drop_not_detected():
    closes = [1000.0] * 30 + path(1000, 960, steps=6)[1:] + [970, 978, 985]
    rows = bars_from_closes(closes, wick=0.01)
    check('v_pattern_shallow', sk.detect_v_pattern(df_from_ohlc(rows)), False)


# ---------------------------------------------------------------- 全体

def test_flat_market_detects_nothing():
    rows = bars_from_closes([100 + np.sin(i / 3) * 0.3 for i in range(120)])
    hits = [p['name'] for p in sk.detect_all_patterns(df_from_ohlc(rows)) if p['detected']]
    print('flat:', hits)
    assert not hits, f'横ばい相場で検出された: {hits}'


if __name__ == '__main__':
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    failed = []
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed.append((t.__name__, str(e)))
            print(f'  -> FAILED: {t.__name__}')
    print()
    if failed:
        print(f'{len(failed)}/{len(tests)} 件失敗:')
        for name, err in failed:
            print(f'  - {name}: {err}')
        raise SystemExit(1)
    print(f'全{len(tests)}件のターゲットテストが成功しました')
