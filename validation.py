#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
validation.py — 検証結果が「偶然ではない」かを確かめるための統計ツール（2026-11-11追加）。

これまでの検証（evaluate_strategy.py など）は「1トレード＝1件」で平均をとっていたが、
同じ日に何銘柄も買う・保有期間が重なるため、トレード同士が独立ではなく、件数が多くても
実質的な情報量はずっと少ない。また、多くの組み合わせを試して一番良いものを選ぶと、
偶然よく見えただけのものを選んでしまう（多重検定・過学習）。そこで：

1. daily_series(): トレードを「日次のポートフォリオ収益」に直す。その日に保有中の全ポジションの
   等金額平均。市場平均（等金額）との差も日次で出す。→ 重なりの問題が消え、日次の時系列として扱える。
2. stationary_bootstrap_indices(): 時系列の自己相関を保ったまま再標本化する（Politis & Romano 1994）。
3. bootstrap_test(): 平均（＝1日あたりの超過収益）の信頼区間と、「本当は0以下」のときに
   これだけ良い結果が偶然出る確率（片側p値）。
4. max_t_adjusted(): 多くの設定を試したうえでの補正p値（White の Reality Check／Romano-Wolf の
   1段階 max-t 法）。全設定を同じ再標本で揃えて「一番良く見える設定が偶然か」を確かめる。
5. holm() / benjamini_hochberg(): 戦略ごとのp値をまとめて補正する。

乱数は固定シード（既定 20261111）なので、同じ入力なら同じ結果になる。
"""

import numpy as np

TRADING_DAYS = 245  # 東証の年間営業日のおおよその数（年率換算用）


# ---------------------------------------------------------------------------
# 1. トレード → 日次系列
# ---------------------------------------------------------------------------

def daily_series(trades, n_dates, mkt_ret, cost_pct=0.1, slots=None):
    """
    trades: [{'g': 銘柄の日付→全体の日付番号の配列, 'O','C': 価格配列, 'e': 約定日のindex,
              'x': 決済日のindex, 'x_at_close': 決済が終値か, 'side': 'LONG'|'SHORT'}]
      約定は e日の始値。決済は x日の始値（x_at_close なら x日の終値）。
    mkt_ret: 全体の日付番号ごとの市場平均（等金額）の日次リターン（小数）。
    cost_pct: 往復コスト（%）。約定日に全額差し引く。
    slots: None なら保有中のポジションに資金を等分（ポジションが1つなら全額）。
           数値 K なら資金を K 枠に分け、1ポジション＝1枠（使っていない枠は現金）。K を超えたら等分。
           シグナルの数が日によって大きく違う戦略では、None だと少数のポジションに資金が集中した日の
           成績が強く効いてしまうため、K 枠のほうが実際の運用に近い（2026-11-11追加）。

    戻り値 dict（いずれも長さ n_dates の配列）:
      'raw'    : 保有中ポジションの等金額平均リターン（ポジションなしの日は0＝現金）
      'excess' : 保有中ポジションの（リターン − 市場平均）の平均（ポジションなしの日は0）
      'count'  : その日に保有中のポジション数
      'entries': その日に約定したトレード数
    """
    sum_r = np.zeros(n_dates)
    cnt = np.zeros(n_dates)
    entries = np.zeros(n_dates)
    cost = cost_pct / 100.0
    for t in trades:
        O, C, g, e, x = t['O'], t['C'], t['g'], t['e'], t['x']
        if x < e:
            continue
        # 日ごとのリターン：約定日は始値→終値、以降は終値→終値、決済日（始値決済）は前日終値→始値
        if x == e:
            if t['x_at_close']:
                r = np.array([C[e] / O[e] - 1])
            else:  # 同じ日の始値で約定・決済（起こらないはずだが念のため）
                r = np.array([0.0])
        else:
            last = x if t['x_at_close'] else x - 1
            seg = C[e:last + 1]
            r = np.empty(x - e + 1)
            r[0] = seg[0] / O[e] - 1
            if last > e:
                r[1:last - e + 1] = seg[1:] / seg[:-1] - 1
            if not t['x_at_close']:
                r[-1] = O[x] / C[x - 1] - 1
        r = np.where(np.isfinite(r), r, 0.0)
        if t['side'] == 'SHORT':
            r = -r
        r[0] -= cost
        gi = g[e:x + 1]
        np.add.at(sum_r, gi, r)
        np.add.at(cnt, gi, 1.0)
        entries[g[e]] += 1
    m = np.asarray(mkt_ret, dtype=float)
    held = cnt > 0
    denom = np.maximum(cnt, slots) if slots else cnt
    raw = np.zeros(n_dates)
    raw[held] = sum_r[held] / denom[held]
    excess = np.zeros(n_dates)
    # SHORT の市場比較は「市場が下がった分を利益」とする（evaluate_strategy.stats と同じ考え方）
    sign = -1.0 if trades and trades[0]['side'] == 'SHORT' else 1.0
    excess[held] = (sum_r[held] - sign * m[held] * cnt[held]) / denom[held]
    return {'raw': raw, 'excess': excess, 'count': cnt, 'entries': entries}


def summarize(x, active=None):
    """日次系列の要約。active（保有中の日のマスク）を渡すと保有日数も出す。"""
    x = np.asarray(x, dtype=float)
    T = len(x)
    if T == 0:
        return {'days': 0}
    mu = x.mean()
    sd = x.std(ddof=1) if T > 1 else 0.0
    curve = np.cumsum(x)
    dd = curve - np.maximum.accumulate(np.concatenate([[0.0], curve]))[1:]
    return {
        'days': int(T),
        'active_days': int(np.sum(active)) if active is not None else None,
        'mean_daily_pct': round(mu * 100, 4),
        'annual_pct': round(mu * TRADING_DAYS * 100, 2),
        'ir_annual': round(mu / sd * np.sqrt(TRADING_DAYS), 2) if sd > 0 else None,
        'total_pct': round(curve[-1] * 100, 2),
        'max_drawdown_pct': round(dd.min() * 100, 2),
    }


# ---------------------------------------------------------------------------
# 2〜4. ブートストラップ
# ---------------------------------------------------------------------------

def stationary_bootstrap_indices(T, n_boot=2000, mean_block=20, seed=20261111):
    """Politis-Romano の定常ブートストラップ。平均 mean_block 日のかたまりで並べ直す。"""
    rng = np.random.default_rng(seed)
    p = 1.0 / mean_block
    idx = np.empty((n_boot, T), dtype=np.int64)
    idx[:, 0] = rng.integers(0, T, size=n_boot)
    jump = rng.random((n_boot, T)) < p
    starts = rng.integers(0, T, size=(n_boot, T))
    for t in range(1, T):
        idx[:, t] = np.where(jump[:, t], starts[:, t], (idx[:, t - 1] + 1) % T)
    return idx


def bootstrap_test(x, idx):
    """
    平均の95%信頼区間と片側p値（帰無仮説：本当の平均は0以下）。
    p値＝「平均0の世界」に平行移動した再標本で、実際の平均以上が出た割合。
    """
    x = np.asarray(x, dtype=float)
    mu = x.mean()
    boot = x[idx].mean(axis=1)
    lo, hi = np.percentile(boot, [2.5, 97.5])
    p = float(np.mean(boot - mu >= mu))
    return {'mean_daily_pct': round(mu * 100, 4), 'ci95_daily_pct': [round(lo * 100, 4), round(hi * 100, 4)],
            'p_value': round(p, 4)}


def max_t_adjusted(X, idx):
    """
    X: (設定数 k, 日数 T) の日次系列。全設定を同じ再標本 idx で揃え、
    各設定の t値（平均÷標準誤差）について「k個の中の最大値が偶然これ以上になる確率」を補正p値とする。
    戻り値: (各設定の t値, 各設定の補正p値, 最良設定の Reality Check p値)
    """
    X = np.asarray(X, dtype=float)
    k, T = X.shape
    mu = X.mean(axis=1)
    sd = X.std(axis=1, ddof=1)
    sd = np.where(sd > 0, sd, np.inf)
    t_obs = np.sqrt(T) * mu / sd
    max_boot = np.full(idx.shape[0], -np.inf)
    for c in range(k):
        if not np.isfinite(sd[c]):
            continue
        tb = np.sqrt(T) * (X[c][idx].mean(axis=1) - mu[c]) / sd[c]
        np.maximum(max_boot, tb, out=max_boot)
    adj = np.array([float(np.mean(max_boot >= t)) for t in t_obs])
    return t_obs, adj, float(np.mean(max_boot >= t_obs.max())) if k else None


# ---------------------------------------------------------------------------
# 5. 多重比較の補正
# ---------------------------------------------------------------------------

def holm(pvals):
    """Holm 法の補正p値（全体で1つでも偽陽性が出る確率を抑える）。"""
    p = np.asarray(pvals, dtype=float)
    m = len(p)
    order = np.argsort(p)
    adj = np.empty(m)
    run = 0.0
    for rank, i in enumerate(order):
        run = max(run, min(1.0, (m - rank) * p[i]))
        adj[i] = run
    return adj


def benjamini_hochberg(pvals):
    """Benjamini-Hochberg 法の q値（「有意」とした中の偽陽性の割合を抑える）。"""
    p = np.asarray(pvals, dtype=float)
    m = len(p)
    order = np.argsort(p)
    adj = np.empty(m)
    run = 1.0
    for rank in range(m - 1, -1, -1):
        i = order[rank]
        run = min(run, p[i] * m / (rank + 1))
        adj[i] = run
    return adj
