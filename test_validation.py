# -*- coding: utf-8 -*-
"""validation.py の単体テスト（python test_validation.py）。"""
import numpy as np

import validation as v


def test_daily_series_matches_trade_return():
    O = np.array([100., 101., 103., 104., 106.])
    C = np.array([100.5, 102., 103.5, 105., 107.])
    g = np.arange(5)
    # 1日目の始値で約定 → 4日目の始値で決済
    t = {'g': g, 'O': O, 'C': C, 'e': 1, 'x': 4, 'x_at_close': False, 'side': 'LONG'}
    s = v.daily_series([t], 5, np.zeros(5), cost_pct=0.0)
    compounded = np.prod(1 + s['raw'][1:5]) - 1
    assert abs(compounded - (O[4] / O[1] - 1)) < 1e-12
    assert s['count'].tolist() == [0, 1, 1, 1, 1] and s['entries'][1] == 1
    # 終値決済
    t2 = dict(t, x=3, x_at_close=True)
    s2 = v.daily_series([t2], 5, np.zeros(5), cost_pct=0.0)
    assert abs(np.prod(1 + s2['raw'][1:4]) - 1 - (C[3] / O[1] - 1)) < 1e-12
    # SHORT は符号反転・市場との差も反転
    mkt = np.full(5, 0.01)
    s3 = v.daily_series([dict(t, side='SHORT')], 5, mkt, cost_pct=0.0)
    s1 = v.daily_series([t], 5, mkt, cost_pct=0.0)
    assert np.allclose(s3['excess'][1:5], -s1['excess'][1:5])


def test_bootstrap_detects_signal_and_noise():
    rng = np.random.default_rng(0)
    T = 500
    idx = v.stationary_bootstrap_indices(T, 1000, 20, seed=1)
    noise = rng.normal(0, 0.01, T)
    noise -= noise.mean()
    assert v.bootstrap_test(noise, idx)['p_value'] > 0.3
    assert v.bootstrap_test(noise + 0.003, idx)['p_value'] < 0.01


def test_max_t_penalizes_many_tries():
    rng = np.random.default_rng(2)
    X = rng.normal(0, 0.01, (200, 400))  # 本当はどれも0
    idx = v.stationary_bootstrap_indices(400, 1000, 10, seed=3)
    t, adj, p_best = v.max_t_adjusted(X, idx)
    raw_best_p = v.bootstrap_test(X[np.argmax(t)], idx)['p_value']
    assert raw_best_p < 0.05      # 1つだけ見ると「有意」に見えるが
    assert p_best > 0.1           # 200通り試したことを補正すると偶然


def test_holm_bh():
    p = [0.01, 0.04, 0.03, 0.5]
    assert np.allclose(v.holm(p), [0.04, 0.09, 0.09, 0.5])
    assert np.allclose(v.benjamini_hochberg(p), [0.04, 0.0533333, 0.0533333, 0.5], atol=1e-6)


if __name__ == '__main__':
    for name, fn in list(globals().items()):
        if name.startswith('test_'):
            fn()
            print('ok', name)
