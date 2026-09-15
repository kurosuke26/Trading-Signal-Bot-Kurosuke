#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_event_strategies.py — event_strategies.py（増配修正・暴落後の仮想売買系列）のテスト。ネットワーク不要。

確認すること
- 増配修正：シグナル日の翌営業日の始値で約定、終値でストップに触れたら翌営業日の始値で決済
- 暴落後：利確・損切り（高値・安値）、寄り付きの窓、同じ日に両方触れたら損切り優先、最長20営業日
- 同じ日足で2回更新しても結果が変わらない（冪等）
- run()：TDnetの開示（テスト用に直接渡す）から、取引可能な銘柄だけ・重複なしで建玉する
- タイトル判定：（増配）を明示した配当予想の修正・剰余金の配当だけを拾う
"""

import copy
import os
import tempfile
from datetime import datetime, timezone

import numpy as np
import pandas as pd

import event_strategies as es


def make_df(closes, start='2026-01-05', spread=0.01, volume=1_000_000, opens=None, highs=None, lows=None):
    idx = pd.bdate_range(start, periods=len(closes))
    c = np.array(closes, dtype=float)
    o = np.array(opens, dtype=float) if opens is not None else np.r_[c[0], c[:-1]]
    h = np.array(highs, dtype=float) if highs is not None else np.maximum(o, c) * (1 + spread)
    l = np.array(lows, dtype=float) if lows is not None else np.minimum(o, c) * (1 - spread)
    return pd.DataFrame({'Open': o, 'High': h, 'Low': l, 'Close': c, 'Volume': volume}, index=idx)


def test_title_classifier():
    ok = ['配当予想の修正（増配）に関するお知らせ', '剰余金の配当（増配）に関するお知らせ',
          '業績予想及び配当予想の修正（増配）に関するお知らせ']
    ng = ['剰余金の配当および次期配当予想（増配）に関するお知らせ', '2026年6月期期末配当予想の修正（無配）に関するお知らせ',
          '株主提案（増配・定款変更）に関する当社取締役会の意見', '2026年３月期　配当予想の修正に関するお知らせ',
          '配当予想の修正（減配）に関するお知らせ']
    assert all(es.is_dividend_hike_revision(t) for t in ok)
    assert not any(es.is_dividend_hike_revision(t) for t in ng)


def test_dividend_hike_entry_and_trailing_exit():
    closes = [100] * 40 + [110, 112, 115, 100, 99]
    df = make_df(closes)
    sig = df.index[39].strftime('%Y-%m-%d')
    p = {'series': es.DIVIDEND_HIKE, 'ticker': 'X', 'signal_date': sig, 'atr_at_signal': 2.0,
         'market_level_at_signal': None, 'state': 'pending_entry'}
    es.update_position(p, df, atr_now=2.0, index_df=None)
    assert p['entry_date'] == df.index[40].strftime('%Y-%m-%d') and p['entry_price'] == 100.0  # 翌営業日の始値（=前日終値の合成データ）
    # 115で高値更新→ストップ 115-6=109、終値100で抵触→翌営業日（99の日）の始値=100で決済
    assert p['state'] == 'closed', p
    assert p['exit_date'] == df.index[44].strftime('%Y-%m-%d') and p['exit_price'] == 100.0
    before = copy.deepcopy(p)
    es.update_position(p, df, atr_now=2.0, index_df=None)
    assert p == before  # 冪等


def test_crash_bracket_rules():
    base = [100] * 40
    # 利確：エントリー日(始値100)の翌日に高値108（利確=100+2*4=108）
    df = make_df(base + [100, 101, 102], highs=[101] * 40 + [101, 108.5, 103], lows=[99] * 40 + [99, 100, 101])
    p = {'series': es.CRASH_REBOUND, 'ticker': 'X', 'signal_date': df.index[39].strftime('%Y-%m-%d'),
         'atr_at_signal': 2.0, 'market_level_at_signal': None, 'state': 'pending_entry'}
    es.update_position(p, df, atr_now=2.0, index_df=None)
    assert p['state'] == 'closed' and p['exit_price'] == 108.0 and p['exit_reason'] == '利確', p

    # 同じ日に利確と損切りの両方 → 損切り（96）優先
    df2 = make_df(base + [100, 101], highs=[101] * 40 + [101, 109], lows=[99] * 40 + [99, 95])
    p2 = dict(p, state='pending_entry', last_processed=None)
    for k in ('entry_date', 'entry_price', 'exit_date', 'exit_price', 'exit_reason', 'return_pct', 'take_profit', 'stop'):
        p2.pop(k, None)
    es.update_position(p2, df2, atr_now=2.0, index_df=None)
    assert p2['exit_price'] == 96.0 and p2['exit_reason'] == '損切り', p2

    # 最長20営業日：動かなければ20日目の終値で決済
    df3 = make_df(base + [100] * 25, highs=[101] * 65, lows=[99] * 65)
    p3 = {'series': es.CRASH_REBOUND, 'ticker': 'X', 'signal_date': df3.index[39].strftime('%Y-%m-%d'),
          'atr_at_signal': 2.0, 'market_level_at_signal': None, 'state': 'pending_entry'}
    es.update_position(p3, df3, atr_now=2.0, index_df=None)
    assert p3['state'] == 'closed' and p3['exit_date'] == df3.index[40 + 19].strftime('%Y-%m-%d'), p3


def test_run_opens_tradeable_hikes_without_duplicates():
    tmp = tempfile.mkdtemp()
    es.EVENT_TRADE_LOG_PATH = os.path.join(tmp, 'log.json')
    es.EVENT_STATE_PATH = os.path.join(tmp, 'state.json')
    liquid = make_df(list(np.linspace(1000, 1100, 80)), volume=200_000)       # 売買代金 約2億円/日
    thin = make_df(list(np.linspace(1000, 1100, 80)), volume=1_000)          # 約100万円/日 → 取引不可
    sig_date = liquid.index[70].strftime('%Y-%m-%d')
    items = [{'id': 'a', 'ticker': 'L.T', 'name': 'L', 'title': '配当予想の修正（増配）', 'disc_date': sig_date, 'disc_time': '15:30', 'url': ''},
             {'id': 'b', 'ticker': 'L.T', 'name': 'L', 'title': '剰余金の配当（増配）', 'disc_date': sig_date, 'disc_time': '15:40', 'url': ''},
             {'id': 'c', 'ticker': 'T.T', 'name': 'T', 'title': '配当予想の修正（増配）', 'disc_date': sig_date, 'disc_time': '15:30', 'url': ''}]
    out = es.run({'L.T': liquid, 'T.T': thin}, today_utc=datetime(2026, 4, 30, tzinfo=timezone.utc),
                 tdnet_items=items, fetch_missing=False)
    tickers = [p['ticker'] for p in out['new_dividend_hikes']]
    assert tickers == ['L.T'], tickers  # 重複（同じ銘柄の2件目）と売買代金不足の銘柄は建てない
    again = es.run({'L.T': liquid, 'T.T': thin}, today_utc=datetime(2026, 4, 30, tzinfo=timezone.utc),
                   tdnet_items=items, fetch_missing=False)
    assert again['new_dividend_hikes'] == []  # 処理済みの開示は再度建てない
    assert out['performance'][es.DIVIDEND_HIKE]['open'] + out['performance'][es.DIVIDEND_HIKE]['closed'] == 1


if __name__ == '__main__':
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    for t in tests:
        t()
        print('ok', t.__name__)
    print(f'全{len(tests)}件のテストが成功しました')
