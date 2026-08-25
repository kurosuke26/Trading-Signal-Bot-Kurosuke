#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_patterns_targeted.py — 各酒田五法パターンを「教科書通りの形」で狙い撃ちして検出できるかを確認する。

test_synthetic.py はランダム生成データでの動作確認（壊れていないか）が目的だったが、
こちらは各detect_*関数が「意図した形が来たときに、確実にTrueを返すか」を検証するための
より厳密なテスト。ロジックの取り違え（例：off-by-oneで絶対に発火しない等）を防ぐ。
"""

import numpy as np
import pandas as pd

import sakata as sk


def df_from_ohlc(rows):
    """rows: list of (open, high, low, close) タプル"""
    idx = pd.date_range('2026-01-01', periods=len(rows), freq='B')
    o, h, l, c = zip(*rows)
    return pd.DataFrame({'Open': o, 'High': h, 'Low': l, 'Close': c}, index=idx)


def test_sanpei_bullish():
    rows = [(100, 105, 99, 104)] * 20  # 前段の水増し（極値検出に影響しないダミー行）
    rows += [(100, 110, 99, 108), (108, 118, 107, 116), (116, 126, 115, 124)]
    df = df_from_ohlc(rows)
    r = sk.detect_sanpei(df)
    print('sanpei_bullish:', r)
    assert r['detected'] and r['direction'] == 'bullish'


def test_sanpei_bearish():
    rows = [(100, 101, 95, 96)] * 20
    rows += [(120, 121, 110, 112), (112, 113, 100, 102), (102, 103, 90, 92)]
    df = df_from_ohlc(rows)
    r = sk.detect_sanpei(df)
    print('sanpei_bearish:', r)
    assert r['detected'] and r['direction'] == 'bearish'


def test_sanku_gap_up():
    rows = [(100, 105, 98, 103)]
    rows += [(110, 115, 109, 113), (120, 125, 119, 123), (130, 135, 129, 133)]
    df = df_from_ohlc(rows)
    r = sk.detect_sanku(df)
    print('sanku_gap_up:', r)
    assert r['detected'] and r['direction'] == 'bearish'  # 上放れ3連続=行き過ぎ→弱気警戒


def test_sanku_gap_down():
    rows = [(100, 102, 95, 97)]
    rows += [(90, 91, 85, 87), (80, 81, 75, 77), (70, 71, 65, 67)]
    df = df_from_ohlc(rows)
    r = sk.detect_sanku(df)
    print('sanku_gap_down:', r)
    assert r['detected'] and r['direction'] == 'bullish'


def test_tasuki_up():
    d1 = (100, 112, 99, 110)   # 陽線
    d2 = (115, 117, 113, 114)  # 窓を開けて始まり(Open115>d1Close110)、陰線で終わるが
                                # Low113はd1High112を上回ったまま＝窓を埋めていない
    df = df_from_ohlc([d1, d2])
    r = sk.detect_tasuki(df)
    print('tasuki_up:', r)
    assert r['detected'] and r['direction'] == 'bullish'


def test_kenuki_bottom():
    rows = [(100, 101, 90, 95), (94, 100, 90.3, 99)]
    df = df_from_ohlc(rows)
    r = sk.detect_kenuki(df)
    print('kenuki_bottom:', r)
    assert r['detected'] and r['direction'] == 'bullish'


def test_danchigai_up():
    rows = [(100, 105, 99, 104), (106, 112, 105, 111), (113, 119, 112, 118), (120, 126, 119, 125)]
    df = df_from_ohlc(rows)
    r = sk.detect_danchigai(df)
    print('danchigai_up:', r)
    assert r['detected'] and r['direction'] == 'bullish'


def test_sanpou_bullish():
    lead = (100, 130, 99, 128)  # 大陽線
    m1 = (120, 124, 115, 118)
    m2 = (118, 121, 114, 117)
    m3 = (117, 122, 113, 119)
    brk = (119, 140, 118, 138)  # レンジの高値(130)を上抜け
    df = df_from_ohlc([lead, m1, m2, m3, brk])
    r = sk.detect_sanpou(df)
    print('sanpou_bullish:', r)
    assert r['detected'] and r['direction'] == 'bullish'


def test_sanzan():
    # 3つのほぼ同水準の山(140/139/141)と、間に2つの谷(安値80)、直近終値がネックライン割れ。
    # detect_sanzan は len(df)>=30 を要求するため、影響しない水増し行(フラット)を先頭に足す。
    # フラット行は極値検出のargmaxが常に先頭indexを返すため、誤って山/谷と判定されない。
    def row(h):
        return (h - 8, h, h - 10, h - 6)

    highs = [90, 95, 100, 140, 100, 95, 90, 100, 139, 95, 90, 100, 141, 95, 88, 80]
    filler = [(100, 101, 99, 100)] * 20
    rows = filler + [row(h) for h in highs] + [(72, 75, 60, 65)]  # 最終行でネックライン(80)割れ
    df = df_from_ohlc(rows)
    r = sk.detect_sanzan(df)
    print('sanzan:', r)
    assert r['detected'] and r['direction'] == 'bearish'


def test_sankawa():
    # 三山の逆パターン：3つのほぼ同水準の谷(60/61/59)と、間に2つの山(高値120)、
    # 直近終値がレジスタンス上抜け。
    def row(l):
        return (l + 6, l + 10, l, l + 8)

    lows = [110, 105, 100, 60, 100, 105, 110, 100, 61, 105, 110, 100, 59, 105, 112, 120]
    filler = [(100, 101, 99, 100)] * 20
    rows = filler + [row(x) for x in lows] + [(128, 140, 126, 138)]  # 最終行でレジスタンス(120)上抜け
    df = df_from_ohlc(rows)
    r = sk.detect_sankawa(df)
    print('sankawa:', r)
    assert r['detected'] and r['direction'] == 'bullish'


def test_v_pattern():
    down = list(np.linspace(1000, 700, 10))
    up = list(np.linspace(700, 980, 8))
    closes = down + up
    rows = [(c, c * 1.01, c * 0.99, c) for c in closes]
    df = df_from_ohlc(rows)
    r = sk.detect_v_pattern(df, lookback=len(rows))
    print('v_pattern:', r)
    assert r['detected'] and r['direction'] == 'bullish'


def test_n_pattern():
    leg1 = list(np.linspace(115, 100, 5))        # 助走の下げ（安値100で底）
    up1 = list(np.linspace(100, 150, 6))[1:]     # 上昇（高値150）。先頭はleg1の底と重複するので除く
    pullback = list(np.linspace(150, 130, 4))    # 押し目（安値130、leg1の安値100より高い＝切り上げ）
    breakout = list(np.linspace(130, 160, 5))    # 直近高値150を上抜け
    closes = leg1 + up1 + pullback + breakout
    rows = [(c, c * 1.005, c * 0.995, c) for c in closes]
    df = df_from_ohlc(rows)
    r = sk.detect_n_pattern(df, lookback=len(rows))
    print('n_pattern:', r)
    assert r['detected'] and r['direction'] == 'bullish'


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
    else:
        print(f'全{len(tests)}件のターゲットテストが成功しました')
