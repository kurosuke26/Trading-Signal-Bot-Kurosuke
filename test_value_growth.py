#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_value_growth.py — 割安（VALUE）／成長（GROWTH）の2本立て（2026-10-08追加）の単体テスト。
ネットワークは使わず、合成データだけで確認する。

使い方: python test_value_growth.py
"""

import sys

import numpy as np
import pandas as pd

import poster
import tracking
import value_growth_scoring as vgs


def _row(t, **kw):
    base = {'ticker': t, 'per': 10.0, 'pbr': 1.0, 'div_yield': 3.0, 'per_forecast': 10.0, 'cash_to_mktcap': 0.2,
            'sales_yoy': 0.0, 'op_yoy': 0.0, 'op_growth_forecast': 0.0, 'eps_growth_forecast': 0.1,
            'value_ratio_20_60': 1.0, 'high_52w_ratio': 0.9, 'ret_120': 0.0}
    base.update(kw)
    return base


def test_per_pbr_tiers():
    assert vgs.per_pbr_points(10) == 1.0
    assert vgs.per_pbr_points(14) == 1.0
    assert abs(vgs.per_pbr_points(22.5) - 0.2) < 1e-9
    assert 0.2 < vgs.per_pbr_points(18) < 0.6
    assert vgs.per_pbr_points(23) == 0.0
    assert np.isnan(vgs.per_pbr_points(-1))
    print('ok test_per_pbr_tiers')


def test_value_ranks_cheaper_higher():
    df = pd.DataFrame([_row('A', per=8, pbr=0.6, cash_to_mktcap=0.6, div_yield=4.0),
                       _row('B', per=15, pbr=1.4, cash_to_mktcap=0.1, div_yield=1.0),
                       _row('C', per=30, pbr=3.0, cash_to_mktcap=0.05, div_yield=0.5)])
    s = vgs.add_scores(df).set_index('ticker')
    assert s.loc['A', 'value_score'] > s.loc['B', 'value_score'] > s.loc['C', 'value_score']
    assert bool(s.loc['A', 'value_gate']) and bool(s.loc['B', 'value_gate']) and not bool(s.loc['C', 'value_gate'])
    print('ok test_value_ranks_cheaper_higher')


def test_growth_gate():
    df = pd.DataFrame([_row('G', sales_yoy=0.2, op_yoy=0.3, op_growth_forecast=0.1),
                       _row('H', sales_yoy=0.2, op_yoy=0.05),                          # 営業益の伸び不足
                       _row('I', sales_yoy=0.2, op_yoy=0.3, op_growth_forecast=-0.1),  # 会社予想が減益
                       _row('J', sales_yoy=0.2, op_yoy=0.3, op_growth_forecast=None)])  # 予想なしは問わない
    s = vgs.add_scores(df).set_index('ticker')
    assert s['growth_gate'].to_dict() == {'G': True, 'H': False, 'I': False, 'J': True}
    print('ok test_growth_gate')


def test_missing_data_reweights():
    df = pd.DataFrame([_row('A', equity_ratio=0.5), _row('B', cash_to_mktcap=None, equity_ratio=0.4)])
    s = vgs.add_scores(df).set_index('ticker')
    assert s['value_score'].notna().all()  # 現金比率が欠けても、残りの配点（PER×PBR・配当・自己資本）で採点
    df2 = pd.DataFrame([{'ticker': 'Z', 'per': 10, 'pbr': 1.0}])
    assert vgs.add_scores(df2)['value_score'].isna().all()  # 判定できる配点が半分未満（PER×PBRの25点のみ）ならNaN
    print('ok test_missing_data_reweights')


def test_price_features():
    idx = pd.bdate_range('2026-01-01', periods=200)
    close = np.linspace(100, 200, 200)
    df = pd.DataFrame({'Close': close, 'Volume': [1000] * 180 + [3000] * 20}, index=idx)
    f = vgs.price_features(df)
    assert f['above_ma50'] is True
    assert abs(f['high_52w_ratio'] - 1.0) < 1e-9
    assert f['ret_120'] > 0 and f['value_ratio_20_60'] > 2.5
    assert vgs.price_features(None)['ret_120'] is None
    print('ok test_price_features')


def _results():
    res = {}
    for i in range(12):
        t = f'{1000 + i}.T'
        res[t] = {'ticker': t, 'name': f'銘柄{i}', 'signal': 'NEUTRAL', 'current_price': 1000.0, 'score': 50,
                  'tech_snapshot': {'atr': 20.0}, 'fundamental_snapshot': {}, 'dividend_yield': 3.0, 'pbr': 0.8,
                  'vg': {'value_score': 90 - i, 'value_gate': True, 'above_ma50': i != 0,
                         'growth_score': 80 - i, 'growth_gate': i % 2 == 0, 'per_pbr_calc': 10.0,
                         'sales_yoy': 0.2, 'op_yoy': 0.3, 'peg': 0.8, 'value_ratio_20_60': 1.5,
                         'high_52w_ratio': 0.95, 'cash_to_mktcap': 0.3}}
    return res


def test_candidates_and_entries():
    res = _results()
    v = tracking.vg_display_candidates(res, 'VALUE', 5)
    assert [r['ticker'] for r in v] == ['1001.T', '1002.T', '1003.T', '1004.T', '1005.T']  # 1000.Tは50日線の下
    g = tracking.vg_display_candidates(res, 'GROWTH', 3)
    assert [r['ticker'] for r in g] == ['1000.T', '1002.T', '1004.T']
    log = []
    n = tracking.open_new_positions(log, res, '2026-10-08', entry_next_open=False)
    sigs = [p['signal'] for p in log]
    assert n == len(log) and sigs.count('VALUE') == tracking.VALUE_TOP_N and sigs.count('GROWTH') == tracking.GROWTH_TOP_N
    assert 'SHORT' not in sigs
    p = next(p for p in log if p['signal'] == 'VALUE')
    assert p['variants']['3.0']['stop'] == 1000 - 20 * 3.0  # 買い方向のストップ（下側）
    assert p['max_hold_bdays'] == tracking.MAX_HOLD_BDAYS_BY_SIGNAL['VALUE']
    # 同じ日にもう一度呼んでも重複して建てない
    assert tracking.open_new_positions(log, res, '2026-10-08') == 0
    print('ok test_candidates_and_entries')


def test_max_hold_closes_position():
    res = _results()
    log = []
    tracking.open_new_positions(log, res, '2026-07-01', entry_next_open=False)
    g = [p for p in log if p['signal'] == 'GROWTH']
    for r in res.values():
        r['current_price'] = 1100.0  # 値上がりしてストップには触れない
    # 20営業日未満ではまだ決済しない
    tracking.update_open_positions(log, res, '2026-07-20')
    assert all(p['status'] == 'open' for p in g)
    tracking.update_open_positions(log, res, '2026-07-29')  # 7/1から20営業日
    assert all(p['status'] == 'closed' for p in g)
    v = g[0]['variants']['3.0']
    assert v['close_reason'] == 'max_hold' and v['return_pct'] == 10.0
    assert all(p['status'] == 'open' for p in log if p['signal'] == 'VALUE')  # VALUEは最長60営業日
    stats = tracking.compute_performance_stats(log)
    assert stats['growth_only']['closed_count'] == len(g) and stats['growth_only']['expectancy_pct'] == 10.0
    assert stats['value_only']['closed_count'] == 0
    assert stats['valuation']['VALUE']['open_count'] == tracking.VALUE_TOP_N
    print('ok test_max_hold_closes_position')


def test_poster_vg_embeds():
    res = _results()
    for r in res.values():
        r['detail'] = 'full'
    embeds = poster.build_vg_embeds(res)
    assert len(embeds) == len(poster.VG_SPECS) == 4
    assert '🎯1.' in embeds[0]['description'] and '1001.T' in embeds[0]['description']
    assert all(len(e['description']) <= 4000 for e in embeds)
    empty = poster.build_vg_embeds({})
    assert all('該当なし' in e['title'] for e in empty)
    print('ok test_poster_vg_embeds')


def test_annotate_without_cache():
    res = _results()
    for r in res.values():
        r.pop('vg')
        r['per'] = 10.0
    n = vgs.annotate(res, {}, '2026-10-08')
    assert n == len(res) and all('vg' in r for r in res.values())
    assert all(r['vg']['value_gate'] for r in res.values())
    print('ok test_annotate_without_cache')


def test_financial_separated():
    df = pd.DataFrame([_row('BANK', per=6, pbr=0.4, cash_to_mktcap=3.0, roe=0.07, sector_name='銀行業'),
                       _row('BANK2', per=10, pbr=1.0, roe=0.10, sector_name='銀行業'),
                       _row('MAKER', per=8, pbr=0.6, cash_to_mktcap=0.5, roe=0.08, sector_name='機械')])
    s = vgs.add_scores(df).set_index('ticker')
    # 金融業は割安・成長の対象外、金融スコアだけが付く
    assert not bool(s.loc['BANK', 'value_gate']) and not bool(s.loc['BANK', 'growth_gate'])
    assert bool(s.loc['BANK', 'financial_gate']) and not bool(s.loc['MAKER', 'financial_gate'])
    assert np.isnan(s.loc['BANK', 'value_parts_cash'])  # 銀行の現金は採点しない
    assert np.isnan(s.loc['MAKER', 'financial_score'])
    # PBR÷ROE：BANK 0.4/0.07=5.7、BANK2 1.0/0.10=10 → BANKの方が稼ぐ力の割に安い
    assert s.loc['BANK', 'financial_score'] > s.loc['BANK2', 'financial_score']
    print('ok test_financial_separated')


def test_sector_relative():
    rows = [_row(f'M{i}', per=10 + i, pbr=1.0, ret_60=0.01 * i, sector_name='機械') for i in range(6)]
    rows += [_row('S0', per=30, pbr=1.0, ret_60=0.5, sector_name='小売業')]  # 1社だけの業種
    s = vgs.add_scores(pd.DataFrame(rows)).set_index('ticker')
    assert s.loc['M0', 'sec_per_pbr_ratio'] < 1 < s.loc['M5', 'sec_per_pbr_ratio']
    assert s.loc['M0', 'value_parts_sec_per_pbr'] > s.loc['M5', 'value_parts_sec_per_pbr']
    assert abs(s.loc['M5', 'sec_ret60_diff'] - 0.025) < 1e-9
    assert np.isnan(s.loc['S0', 'sec_per_pbr_ratio'])  # 業種の銘柄が少ないと比較しない
    print('ok test_sector_relative')


def test_value_weights_less_concentrated():
    pe_pb = sum(w for k, w in vgs.VALUE_WEIGHTS.items() if k in ('per_pbr', 'sec_per_pbr', 'pbr', 'per'))
    assert sum(vgs.VALUE_WEIGHTS.values()) == 100 and pe_pb <= 50
    print('ok test_value_weights_less_concentrated')


def test_roman2_gate_and_watchlist(tmp=None):
    import os
    import tempfile
    df = pd.DataFrame([_row('R1', mktcap_oku=300, op_revision_from_initial=0.2, ret_120=0.5, sector_name='機械'),
                       _row('R2', mktcap_oku=300, op_revision_from_initial=0.0, sector_name='機械'),
                       _row('R3', mktcap_oku=5000, op_revision_from_initial=0.2, sector_name='機械')])
    s = vgs.add_scores(df).set_index('ticker')
    assert s['roman2_gate'].to_dict() == {'R1': True, 'R2': False, 'R3': False}
    res = _results()
    res['1000.T']['vg'].update(roman2_gate=True, roman2_score=77.0, mktcap_oku=300, op_revision_from_initial=0.2)
    path = os.path.join(tempfile.mkdtemp(), 'roman.json')
    assert tracking.record_roman_watchlist(res, '2026-10-08', path) == 1
    assert tracking.record_roman_watchlist(res, '2026-10-08', path) == 1  # 同じ日は上書き
    assert tracking.record_roman_watchlist(res, '2026-10-09', path) == 1
    import json
    hist = json.load(open(path, encoding='utf-8'))
    assert [h['date'] for h in hist] == ['2026-10-08', '2026-10-09'] and hist[0]['picks'][0]['ticker'] == '1000.T'
    # ロマン枠は仮想売買しない
    log = []
    tracking.open_new_positions(log, res, '2026-10-08')
    assert all(p['signal'] != 'ROMAN' for p in log)
    print('ok test_roman2_gate_and_watchlist')


def test_illiquid_excluded():
    res = _results()
    res['1001.T']['vg']['avg_value_20'] = 1_000_000  # 1日平均100万円しか売買されていない
    res['1002.T']['vg']['avg_value_20'] = 100_000_000
    v = [r['ticker'] for r in tracking.vg_display_candidates(res, 'VALUE', 3)]
    assert '1001.T' not in v and v[0] == '1002.T'
    print('ok test_illiquid_excluded')


def test_save_daily_bars():
    import os
    import tempfile
    import collector
    idx = pd.bdate_range('2026-10-01', periods=5)
    h = {'A.T': pd.DataFrame({'Open': [1, 2, 3, 4, 5], 'High': [2, 3, 4, 5, 6], 'Low': [0.5] * 5,
                              'Close': [1.5, 2.5, 3.5, 4.5, 5.5], 'Volume': [100] * 5}, index=idx),
         'B.T': None}
    d = tempfile.mkdtemp()
    n = collector.save_daily_bars(h, {'A.T': {'per': 10.0, 'pbr': 0.8}}, '2026-10-08', out_dir=d, keep_days=3)
    assert n == 3
    df = pd.read_csv(os.path.join(d, '2026', '2026-10-08.csv.gz'))
    assert list(df['date']) == ['2026-10-05', '2026-10-06', '2026-10-07'] and df['close'].iloc[-1] == 5.5
    assert df['per'].isna().sum() == 2 and df['per'].iloc[-1] == 10.0  # PER等は最新の日の行だけ
    print('ok test_save_daily_bars')


if __name__ == '__main__':
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    for t in tests:
        t()
    print(f'全{len(tests)}件のテストが成功しました')
    sys.exit(0)
