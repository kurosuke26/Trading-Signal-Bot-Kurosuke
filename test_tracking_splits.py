#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_tracking_splits.py — 株式分割・配当調整・配信データ異常への対応の単体テスト（2026-09-23追加）。

株価は分割・配当調整済み（auto_adjust=True）で取っているため、分割が起きると過去の株価が遡って
調整される。記録済みの取得価格・ストップを同じ水準に揃えないと、ロングは即ストップ、
ショートは含み益が倍に見える。ネットワークは使わず、合成データだけで確認する。

使い方: python test_tracking_splits.py
"""

import sys

import pandas as pd

import event_strategies
import tracking


def make_hist(dates, closes, opens=None):
    idx = pd.to_datetime(dates)
    o = opens if opens is not None else closes
    return pd.DataFrame({'Open': o, 'High': [c * 1.01 for c in closes], 'Low': [c * 0.99 for c in closes],
                         'Close': closes, 'Volume': [100000] * len(closes)}, index=idx)


def make_position(signal, entry_price, atr, entry_date='2026-09-01'):
    log = []
    results = {'X.T': {'ticker': 'X.T', 'name': 'テスト', 'signal': signal, 'current_price': entry_price,
                       'tech_snapshot': {'atr': atr}, 'score': 99, 'per_pbr': 99,
                       'sector_relative': {'momentum_ok': True}}}
    tracking.open_new_positions(log, results, entry_date)
    assert len(log) == 1, '前提：テスト用のポジションが1件開くこと'
    return log


def primary(p):
    return p['variants'][tracking.PRIMARY_VARIANT_KEY_BY_SIGNAL[p['signal']]]


def test_long_split_does_not_trigger_stop():
    """1:2分割の翌日、株価が半分になってもロングが決済されないこと（取得価格・ストップが揃う）。"""
    log = make_position('LONG', 1000.0, 20.0)
    hist = make_hist(['2026-09-01', '2026-09-02'], [500.0, 505.0])  # 分割後に遡って調整された系列
    results = {'X.T': {'current_price': 505.0, 'tech_snapshot': {'atr': 10.0}}}
    closed = tracking.update_open_positions(log, results, '2026-09-02', {'X.T': hist})
    p = log[0]
    assert closed == 0, f'分割で誤決済された: {primary(p)}'
    assert p['entry_price'] == 500.0, p['entry_price']
    assert p['atr_at_entry'] == 10.0, p['atr_at_entry']
    assert p['adjustments'] == [{'date': '2026-09-02', 'factor': 0.5}], p.get('adjustments')
    assert primary(p)['status'] == 'open'
    print('ok test_long_split_does_not_trigger_stop')


def test_short_split_does_not_inflate_profit():
    """1:2分割のショートで、含み損益が2倍に膨らまないこと。"""
    log = make_position('SHORT', 1000.0, 20.0)
    hist = make_hist(['2026-09-01', '2026-09-02'], [500.0, 490.0])
    tracking.update_open_positions(log, results_for(490.0), '2026-09-02', {'X.T': hist})
    p = log[0]
    _, _, pnl = tracking.position_pnl_yen('SHORT', p['entry_price'], p['last_price'], lot=100)
    assert p['entry_price'] == 500.0 and p['last_price'] == 490.0, (p['entry_price'], p['last_price'])
    assert pnl == 1000.0, pnl  # (500-490)×100株。調整前なら (1000-490)×100=51,000円に見えてしまう
    print('ok test_short_split_does_not_inflate_profit')


def test_dividend_adjustment_is_applied():
    """配当落ちによる遡及調整（数%）も同じ仕組みで揃えること（検証と同じ総収益ベースになる）。"""
    log = make_position('LONG', 1000.0, 20.0)
    hist = make_hist(['2026-09-01', '2026-09-02'], [980.0, 1000.0])
    tracking.update_open_positions(log, results_for(1000.0), '2026-09-02', {'X.T': hist})
    assert log[0]['entry_price'] == 980.0, log[0]['entry_price']
    print('ok test_dividend_adjustment_is_applied')


def test_no_change_when_series_matches():
    """調整が無ければ記録は書き換えないこと（無用な履歴を残さない）。"""
    log = make_position('LONG', 1000.0, 20.0)
    hist = make_hist(['2026-09-01', '2026-09-02'], [1000.0, 1010.0])
    tracking.update_open_positions(log, results_for(1010.0), '2026-09-02', {'X.T': hist})
    p = log[0]
    assert p['entry_price'] == 1000.0 and 'adjustments' not in p, p
    print('ok test_no_change_when_series_matches')


def test_absurd_price_is_skipped():
    """配信データの異常（例：1909.Tが約163億円）では決済判定を見送り、記録に残すこと。"""
    log = make_position('SHORT', 3705.0, 50.0)
    stop_before = primary(log[0])['stop']
    hist = make_hist(['2026-09-01', '2026-09-02'], [3705.0, 1.628e10])
    closed = tracking.update_open_positions(log, results_for(1.628e10), '2026-09-02', {'X.T': hist})
    p = log[0]
    assert closed == 0, '異常値でショートが決済されてはいけない'
    assert p['entry_price'] == 3705.0 and primary(p)['stop'] == stop_before, (p['entry_price'], primary(p))
    assert p['price_anomaly']['date'] == '2026-09-02', p.get('price_anomaly')
    assert p['last_price'] == 3705.0 and p['last_price_date'] == '2026-09-01', \
        '異常値を評価用の終値として上書きしないこと'
    # 評価（円換算）でも除外されること
    val = tracking.compute_valuation([dict(p, last_price=1.628e10)])
    assert val['SHORT']['bad_price_count'] == 1, val['SHORT']
    print('ok test_absurd_price_is_skipped')


def test_event_position_split_adjustment():
    """別枠（増配ルール）のポジションも、分割後に取得価格・ストップが揃うこと。"""
    p = {'ticker': 'X.T', 'series': event_strategies.DIVIDEND_HIKE, 'state': 'open', 'signal_date': '2026-08-31',
         'entry_date': '2026-09-01', 'entry_price': 1000.0, 'stop': 940.0, 'atr_at_signal': 20.0,
         'last_processed': '2026-09-01', 'held_days': 1}
    df = make_hist(['2026-09-01', '2026-09-02'], [500.0, 505.0], opens=[500.0, 502.0])
    event_strategies.update_position(p, df, atr_now=10.0, index_df=None)
    assert p['entry_price'] == 500.0 and p['adjustments'] == [0.5], (p['entry_price'], p.get('adjustments'))
    assert p['state'] == 'open', f"分割で誤決済された: {p}"
    assert p['stop'] <= 505.0, p['stop']
    print('ok test_event_position_split_adjustment')


def test_delisted_position_is_closed():
    """株価が長く取れないままの銘柄（上場廃止・TOB）は、最後に取れた終値で決済されること。"""
    log = make_position('SHORT', 1000.0, 20.0)
    p = log[0]
    p['last_price'], p['last_price_date'] = 900.0, '2026-09-02'
    closed = tracking.update_open_positions(log, {}, '2026-09-18')  # データが取れない日が続いた状態
    assert closed == 1 and p['status'] == 'closed', (closed, p['status'])
    assert primary(p)['close_reason'] == 'delisted' and primary(p)['close_price'] == 900.0, primary(p)
    assert primary(p)['return_pct'] == 10.0, primary(p)  # SHORT 1000→900
    print('ok test_delisted_position_is_closed')


def test_missing_data_is_carried_over():
    """データ欠損が数日なら決済せず持ち越すこと。"""
    log = make_position('SHORT', 1000.0, 20.0)
    log[0]['last_price_date'] = '2026-09-16'
    closed = tracking.update_open_positions(log, {}, '2026-09-18')
    assert closed == 0 and log[0]['status'] == 'open'
    print('ok test_missing_data_is_carried_over')


def results_for(price, atr=10.0):
    return {'X.T': {'current_price': price, 'tech_snapshot': {'atr': atr}}}


def run():
    tests = [test_long_split_does_not_trigger_stop, test_short_split_does_not_inflate_profit,
             test_dividend_adjustment_is_applied, test_no_change_when_series_matches,
             test_absurd_price_is_skipped, test_event_position_split_adjustment,
             test_delisted_position_is_closed, test_missing_data_is_carried_over]
    for t in tests:
        t()
    print(f'全{len(tests)}件のテストが成功しました')
    return 0


if __name__ == '__main__':
    sys.exit(run())
