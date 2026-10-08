"""
【2026-10-08追加】本番の仮想売買を「翌営業日の始値」で約定・決済することのテスト（ネットワーク不要）。
    python test_next_open.py
"""
import pandas as pd

import tracking


def hist(rows):
    """rows: [(日付, 始値, 終値), ...]"""
    idx = pd.to_datetime([r[0] for r in rows])
    return pd.DataFrame({'Open': [r[1] for r in rows], 'High': [max(r[1], r[2]) for r in rows],
                         'Low': [min(r[1], r[2]) for r in rows], 'Close': [r[2] for r in rows],
                         'Volume': [100000] * len(rows)}, index=idx)


def results(price, atr=10.0):
    return {'X.T': {'ticker': 'X.T', 'name': 'テスト', 'signal': 'LONG', 'current_price': price,
                    'tech_snapshot': {'atr': atr}, 'score': 99, 'per_pbr': 10,
                    'sector_relative': {'momentum_ok': True}}}


def primary(p):
    return p['variants'][tracking.PRIMARY_VARIANT_KEY_BY_SIGNAL[p['signal']]]


def opened_log():
    log = []
    saved, tracking.SHORT_NEW_ENTRIES = tracking.SHORT_NEW_ENTRIES, False
    try:
        tracking.open_new_positions(log, results(1000.0), '2026-10-01')
    finally:
        tracking.SHORT_NEW_ENTRIES = saved
    return [p for p in log if p['signal'] == 'LONG']


def test_signal_day_is_pending():
    log = opened_log()
    assert len(log) == 1
    p = log[0]
    assert p['status'] == 'pending_entry' and p['entry_price'] is None and p['signal_price'] == 1000.0
    # 約定待ちの間は同じ銘柄を重ねて建てない
    assert tracking._has_open(log, 'X.T', 'LONG')
    stats = tracking.compute_performance_stats(log)
    assert stats['pending_entries'] == 1 and stats['open_positions'] == 0
    print('ok test_signal_day_is_pending')


def test_fills_at_next_open():
    log = opened_log()
    h = hist([('2026-10-01', 990, 1000), ('2026-10-02', 1010, 1020)])
    tracking.update_open_positions(log, results(1020.0), '2026-10-02', {'X.T': h})
    p = log[0]
    assert p['status'] == 'open' and p['entry_date'] == '2026-10-02' and p['entry_price'] == 1010.0, p
    # 初期ストップは約定価格（始値）−ATR×3.0。当日の終値で切り上げ（1020−30=990）
    assert primary(p)['stop'] == 990.0, primary(p)
    assert not p.get('adjustments'), '始値と終値の差を分割と誤認しないこと'
    print('ok test_fills_at_next_open')


def test_exit_at_next_open_after_close_breach():
    log = opened_log()
    h = hist([('2026-10-01', 990, 1000), ('2026-10-02', 1010, 1020), ('2026-10-05', 1000, 980)])
    tracking.update_open_positions(log, results(1020.0), '2026-10-02', {'X.T': h})
    # 10/5の終値980がストップ990を下回る → この日は決済せず「翌営業日の始値で手仕舞い」待ち
    closed = tracking.update_open_positions(log, results(980.0), '2026-10-05', {'X.T': h})
    p = log[0]
    assert closed == 0 and primary(p)['status'] == 'exit_pending' and p['status'] == 'open', primary(p)
    # 10/6の始値970で決済
    h2 = hist([('2026-10-01', 990, 1000), ('2026-10-02', 1010, 1020), ('2026-10-05', 1000, 980),
               ('2026-10-06', 970, 975)])
    closed = tracking.update_open_positions(log, results(975.0), '2026-10-06', {'X.T': h2})
    v = primary(p)
    assert closed == 1 and p['status'] == 'closed', v
    assert v['close_date'] == '2026-10-06' and v['close_price'] == 970.0
    assert v['return_pct'] == round((970 - 1010) / 1010 * 100, 2)
    print('ok test_exit_at_next_open_after_close_breach')


def test_pending_expires_without_open_price():
    log = opened_log()
    h = hist([('2026-10-01', 990, 1000)])  # 翌日以降の日足が取れない
    tracking.update_open_positions(log, results(1000.0), '2026-10-02', {'X.T': h})
    assert log[0]['status'] == 'pending_entry'
    tracking.update_open_positions(log, results(1000.0), '2026-10-12', {'X.T': h})
    assert log[0]['status'] == 'cancelled'
    assert not tracking._has_open(log, 'X.T', 'LONG')
    print('ok test_pending_expires_without_open_price')


def test_legacy_mode_unchanged():
    """旧backtest.py用：日足を渡さない呼び出しは従来どおり当日終値で建て・決済する。"""
    log = []
    tracking.open_new_positions(log, results(1000.0), '2026-10-01', entry_next_open=False)
    p = [x for x in log if x['signal'] == 'LONG'][0]
    assert p['status'] == 'open' and p['entry_price'] == 1000.0
    closed = tracking.update_open_positions(log, results(960.0), '2026-10-02')
    assert closed == 1 and primary(p)['close_price'] == 960.0
    print('ok test_legacy_mode_unchanged')


def run():
    tests = [test_signal_day_is_pending, test_fills_at_next_open, test_exit_at_next_open_after_close_breach,
             test_pending_expires_without_open_price, test_legacy_mode_unchanged]
    for t in tests:
        t()
    print(f'全{len(tests)}件のテストが成功しました')


if __name__ == '__main__':
    run()


def test_holdings_report_sections():
    import holdings_report as hr
    log = opened_log()
    h = hist([('2026-10-01', 990, 1000), ('2026-10-02', 1010, 1020), ('2026-10-05', 1000, 980)])
    tracking.update_open_positions(log, results(1020.0), '2026-10-02', {'X.T': h})
    c = hr.classify(log)
    assert len(c['near']) + len(c['hold']) == 1 and not c['exit']
    tracking.update_open_positions(log, results(980.0), '2026-10-05', {'X.T': h})
    c = hr.classify(log, {'X.T': 'SHORT'})
    assert len(c['exit']) == 1 and 'ストップ' in c['exit'][0]['reason'] and c['exit'][0]['caution']
    # 手仕舞い待ちでない保有中のロングにSHORT判定 → ⚠️撤退検討
    log2 = opened_log()
    tracking.update_open_positions(log2, results(1020.0), '2026-10-02', {'X.T': h})
    c2 = hr.classify(log2, {'X.T': 'SHORT'})
    assert len(c2['caution']) == 1 and not c2['near'] and not c2['hold']
    assert '撤退検討' in hr.build_holdings_payload(log2, {'X.T': 'SHORT'}, '2026-10-02')['embeds'][0]['title']
    assert not tracking.SHORT_NEW_ENTRIES, 'SHORTの新規仮想エントリーは既定で停止'
    payload = hr.build_holdings_payload(log, {}, '2026-10-05')
    assert '手仕舞い（1件）' in payload['embeds'][0]['title']
    print('ok test_holdings_report_sections')


if __name__ == '__main__':
    test_holdings_report_sections()
