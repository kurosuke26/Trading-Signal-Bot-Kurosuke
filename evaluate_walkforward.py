#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
evaluate_walkforward.py — 「検証の土台」：walk-forward と統計的な有意性の確認（2026-11-11追加）。

evaluate_strategy.py は期間を前半／後半の2つに分けて1回だけ確かめていた。ここでは：

1. walk-forward（ずらしながら何度も確かめる）
   期間を約3か月（TEST_LEN営業日）ずつに区切り、各区間について
     「その区間より前のデータだけ」で設定（スコアの下限・ATR倍率）を選ぶ →
     その区間で選んだ設定のまま運用した結果を記録する
   を繰り返し、全区間の結果をつなげる。酒田五法の精度（スコアの重み）もその都度、区間より前の
   検出だけで測り直す。→ どの時点でも「その時点で手に入った情報だけ」で運用した成績になる。

2. 日次の成績に直す（validation.daily_series）
   1トレード＝1件の平均ではなく、保有中の全ポジションの等金額平均を日次で出し、市場平均（取引可能な
   全銘柄の等金額平均）との差をとる。トレードの重なりで件数が水増しされる問題を避ける。

3. 偶然かどうかの確認（validation.py）
   - 各戦略の walk-forward 成績：ブートストラップの95%信頼区間とp値、戦略数ぶんの補正（Holm・BH）
   - 全期間で試した全設定（戦略×スコア下限×ATR倍率）：一番良く見える設定が偶然かを
     Reality Check（max-t 法）で確認
   - コスト（往復0.1%／0.3%／0.5%）を変えても残るか

売買のルール（翌営業日の始値で約定・終値がストップに抵触したら翌営業日の始値で決済・上場廃止の扱い・
取引できない銘柄の除外）は evaluate_strategy.py をそのまま使う（本番コードは変更しない）。

【実行方法】
  python evaluate_walkforward.py --max-tickers 200      # 動作確認
  python evaluate_walkforward.py                        # 全銘柄（特徴量は data/backtest_out/wf_features.pkl に保存し再利用）
出力: data/backtest_out/walkforward_eval.json / .md
"""

import argparse
import json
import os
import pickle
from collections import defaultdict
from multiprocessing import Pool

import numpy as np

import evaluate_strategy as es
import repro
import validation as v
from evaluate_sakata import load_eval_universe

TRAIN_MIN = 126        # 最初の区間の前に必要な学習期間（営業日・約6か月）
TEST_LEN = 63          # 1区間の長さ（営業日・約3か月）
MIN_ENTRIES_TRAIN = 30  # 学習期間にこれだけ約定がない設定は選ばない
COSTS = [0.1, 0.3, 0.5]  # 往復コスト（%）
N_BOOT = 2000
MEAN_BLOCK = 20
PROD = {'threshold': 75, 'atr': 3.0}  # 本番の設定（tracking.py: LONG_ENTRY_SCORE_THRESHOLD・ATR_MULTIPLIER_BY_SIGNAL）
# 売りパターンでの除外（2026-11-11追加）：LONG候補のうち、直近N営業日（シグナル日を含む）に酒田五法の
# 売りパターンがどれか1つでも出た銘柄を外し、次点の銘柄で上位5件を埋める。
# 「どのパターンか」はあえて絞らない（前回の結果を見て効いたパターンだけ選ぶと、その結果自体を当てにいく形になるため）。
EXCLUDE_LOOKBACK = {'なし': 0, '当日': 1, '直近5日': 5}


# ---------------------------------------------------------------------------
# 準備：特徴量・全体の日付・市場平均
# ---------------------------------------------------------------------------

def load_features(max_tickers, workers, cache_path):
    universe = sorted(load_eval_universe(max_tickers))
    key = (tuple(universe), es.LOOKBACK_ROWS, es.MIN_ROWS)
    if cache_path and os.path.exists(cache_path):
        with open(cache_path, 'rb') as f:
            cached = pickle.load(f)
        if cached.get('key') == key:
            print(f'[wf] 特徴量をキャッシュから読み込み: {cache_path}', flush=True)
            return universe, cached['data']
    print(f'[wf] 特徴量の計算: {len(universe)}銘柄 / workers={workers}', flush=True)
    data = []
    with Pool(workers) as pool:
        for k, res in enumerate(pool.imap(es.extract_ticker, universe, chunksize=2)):
            if res:
                data.append(res)
            if (k + 1) % 500 == 0:
                print(f'[wf] {k + 1}/{len(universe)} 銘柄完了', flush=True)
    data.sort(key=lambda td: td['t'])
    if cache_path:
        with open(cache_path, 'wb') as f:
            pickle.dump({'key': key, 'data': data}, f, protocol=pickle.HIGHEST_PROTOCOL)
    return universe, data


def build_calendar(data):
    """全体の日付・各銘柄の日付番号・市場平均（前日に取引可能だった銘柄の等金額平均の日次リターン）。"""
    dates = sorted({d for td in data for d in td['dates']})
    pos = {d: k for k, d in enumerate(dates)}
    sum_r = np.zeros(len(dates))
    cnt = np.zeros(len(dates))
    for td in data:
        g = np.array([pos[d] for d in td['dates']], dtype=np.int64)
        td['g'] = g
        td['pos'] = {d: k for k, d in enumerate(td['dates'])}
        C = td['C']
        trad = np.zeros(len(C), dtype=bool)
        for row in td['rows']:
            trad[row[0]] = row[6]
        r = C[1:] / C[:-1] - 1
        ok = trad[:-1] & np.isfinite(r)
        np.add.at(sum_r, g[1:][ok], r[ok])
        np.add.at(cnt, g[1:][ok], 1.0)
    mkt = np.divide(sum_r, cnt, out=np.zeros_like(sum_r), where=cnt > 0)
    return dates, mkt


# ---------------------------------------------------------------------------
# シミュレーション → 日次系列
# ---------------------------------------------------------------------------

def simulate_to_legs(data, cands_by_ti, side, m):
    """evaluate_strategy.simulate の結果を、日次系列を作るための形（約定日・決済日のindex）に直す。"""
    legs = []
    for ti in sorted(cands_by_ti):
        td = data[ti]
        for t in es.simulate(td, sorted(set(cands_by_ti[ti])), side, m):
            i = td['pos'][t['d']]
            x = i + t['hold']
            legs.append({'g': td['g'], 'O': td['O'], 'C': td['C'], 'e': i + 1, 'x': x,
                         'x_at_close': bool(t['open_end'] or t['delisted']), 'side': side})
    return legs


def series_for(legs, n_dates, mkt, costs=COSTS):
    out = {}
    for c in costs:
        s = v.daily_series(legs, n_dates, mkt, cost_pct=c)
        out[c] = s
    return out


def classify_all(data, accuracy):
    """各日のLONG候補（スコア付き）とSHORT候補（PER×PBR）を、与えた酒田五法の精度で判定する。"""
    by_date_long, by_date_short = defaultdict(list), defaultdict(list)
    for ti, td in enumerate(data):
        for row in td['rows']:
            if not row[6]:
                continue
            signal, score = es.classify(row, accuracy)
            d = td['dates'][row[0]]
            if signal == 'LONG':
                by_date_long[d].append((score if score is not None else -1, td['t'], row[0], ti))
            elif signal == 'SHORT':
                by_date_short[d].append((row[2] if row[2] is not None else 0, td['t'], row[0], ti))
    return by_date_long, by_date_short


def bearish_masks(data):
    """{直近N日: [銘柄ごとの bool 配列]}。i日目までの直近N営業日（i日目を含む）に売りパターンが出ていれば True。"""
    out = {}
    for lb in sorted(set(EXCLUDE_LOOKBACK.values()) - {0}):
        masks = []
        for td in data:
            b = np.zeros(len(td['C']), dtype=np.int64)
            for row in td['rows']:
                if any(d == 'bearish' for _, d in row[5]):
                    b[row[0]] = 1
            cs = np.concatenate([[0], np.cumsum(b)])
            k = np.arange(len(b))
            masks.append((cs[k + 1] - cs[np.maximum(0, k - lb + 1)]) > 0)
        out[lb] = masks
    return out


def long_candidates(by_date_long, thr, excl=None):
    """excl（銘柄ごとの bool 配列）が True の銘柄×日は外し、残りから上位 LONG_TOP_N 件を選ぶ。"""
    cands = defaultdict(list)
    for d in sorted(by_date_long):
        pool = [x for x in by_date_long[d] if x[0] >= thr and not (excl is not None and excl[x[3]][x[2]])]
        picked = sorted(pool, key=lambda x: (-x[0], x[1]))[:es.LONG_TOP_N]
        for _, _, i, ti in picked:
            cands[ti].append(i)
    return cands


def short_candidates(by_date_short):
    cands = defaultdict(list)
    for d in sorted(by_date_short):
        for _, _, i, ti in sorted(by_date_short[d], key=lambda x: (-x[0], x[1]))[:es.SHORT_TOP_N]:
            cands[ti].append(i)
    return cands


def fixed_event_candidates(data):
    """スコアに依存しない（＝精度の測り直しが不要な）シグナル：酒田五法の各パターン・出来高急増。"""
    fams = {}
    sak = defaultdict(lambda: defaultdict(list))
    vol = {r: defaultdict(list) for r in es.VOLUME_SURGE_RATIOS}
    for ti, td in enumerate(data):
        for row in td['rows']:
            if not row[6]:
                continue
            i = row[0]
            for name, direction in row[5]:
                sak[(name, direction)][ti].append(i)
            for k, r in enumerate(es.VOLUME_SURGE_RATIOS):
                if row[7][k]:
                    vol[r][ti].append(i)
    for (name, direction), c in sorted(sak.items()):
        side = 'LONG' if direction == 'bullish' else 'SHORT'
        fams[f'酒田五法：{name}（{"買い" if side == "LONG" else "売り"}）'] = (side, c)
    for r in es.VOLUME_SURGE_RATIOS:
        fams[f'出来高急増{r:g}倍・株価横ばい（買い）'] = ('LONG', vol[r])
    return fams


# ---------------------------------------------------------------------------
# walk-forward
# ---------------------------------------------------------------------------

def make_folds(dates, first_signal_k):
    folds = []
    s = first_signal_k + TRAIN_MIN
    while s < len(dates):
        e = min(s + TEST_LEN, len(dates))
        if e - s >= 20:
            folds.append((s, e))
        s = e
    return folds


def pick_config(cfg_series, train_lo, train_hi, cost):
    """学習期間の「市場との差」の年率IR（平均÷ばらつき）が最大の設定。約定がMIN_ENTRIES_TRAIN未満の設定は除く。"""
    best = None
    for key, s in cfg_series.items():
        ex = s[cost]['excess'][train_lo:train_hi]
        n_ent = s[cost]['entries'][train_lo:train_hi].sum()
        if n_ent < MIN_ENTRIES_TRAIN or len(ex) < 2:
            continue
        sd = ex.std(ddof=1)
        ir = ex.mean() / sd if sd > 0 else -np.inf
        cand = (ir, key)
        if best is None or cand > best:
            best = cand
    return best[1] if best else None


def run_family_wf(name, folds, first_k, n_dates, get_cfg_series, cost):
    """get_cfg_series(fold_start) -> {設定: {cost: 系列}}（その区間の前の情報だけで作った候補群）。"""
    oos = {k: np.zeros(n_dates) for k in ('excess', 'raw', 'count')}
    picks = []
    for (s, e) in folds:
        cfgs = get_cfg_series(s)
        key = pick_config(cfgs, first_k, s, cost)
        picks.append({'test_start_k': s, 'test_end_k': e, 'picked': key})
        if key is None:
            continue  # 選べる設定がない区間は現金（成績0）
        for k in oos:
            oos[k][s:e] = cfgs[key][cost][k][s:e]
    return oos, picks


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--max-tickers', type=int, default=0)
    ap.add_argument('--workers', type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument('--out', default='data/backtest_out/walkforward_eval')
    ap.add_argument('--cache', default='data/backtest_out/wf_features.pkl')
    args = ap.parse_args()

    universe, data = load_features(args.max_tickers, args.workers, args.cache or None)
    dates, mkt = build_calendar(data)
    n_dates = len(dates)
    first_signal = min(td['dates'][td['rows'][0][0]] for td in data if td['rows'])
    first_k = dates.index(first_signal)
    folds = make_folds(dates, first_k)
    print(f'[wf] 期間 {dates[first_k]}〜{dates[-1]} / 区間 {len(folds)}個: '
          + ', '.join(f'{dates[s]}〜{dates[e - 1]}' for s, e in folds), flush=True)
    oos_lo = folds[0][0]

    # --- 区間ごとに測り直す酒田五法の精度 → スコア依存の戦略（LONG・SHORT）の候補 ---
    score_cache = {}
    bear = bearish_masks(data)

    def score_family_series(fold_start):
        """fold_start より前の検出で測った精度を使い、LONG（スコア下限×ATR）とSHORT（ATR）の全設定の系列。"""
        if fold_start in score_cache:
            return score_cache[fold_start]
        acc, _ = es.measure_accuracy(data, dates[fold_start])
        bl, bs = classify_all(data, acc)
        long_cfg, short_cfg = {}, {}
        for mode, lb in EXCLUDE_LOOKBACK.items():
            for thr in es.LONG_SCORE_THRESHOLDS:
                cands = long_candidates(bl, thr, bear[lb] if lb else None)
                for m in es.ATR_GRID:
                    long_cfg[(mode, thr, m)] = series_for(simulate_to_legs(data, cands, 'LONG', m), n_dates, mkt)
        sc = short_candidates(bs)
        for m in es.ATR_GRID:
            short_cfg[m] = series_for(simulate_to_legs(data, sc, 'SHORT', m), n_dates, mkt)
        score_cache[fold_start] = (long_cfg, short_cfg, acc)
        print(f'[wf] {dates[fold_start]} 時点の精度で候補を作成（LONG {len(long_cfg)}設定・SHORT {len(short_cfg)}設定）',
              flush=True)
        return score_cache[fold_start]

    # 区間ごとの候補を先に作り、区間をまたいで使い回す（コスト別の選定でも同じ）
    for s, _ in folds:
        score_family_series(s)

    # --- スコアに依存しない戦略（全期間で1回だけ作ればよい） ---
    fixed = {}
    for name, (side, cands) in fixed_event_candidates(data).items():
        fixed[name] = (side, {m: series_for(simulate_to_legs(data, cands, side, m), n_dates, mkt) for m in es.ATR_GRID})
    print(f'[wf] スコアに依存しない戦略 {len(fixed)}個の系列を作成', flush=True)

    def long_family(mode):
        def fn(s):
            lc = score_family_series(s)[0]
            return {k: x for k, x in lc.items() if mode is None or k[0] == mode}
        return fn

    families = {}
    for mode in EXCLUDE_LOOKBACK:
        label = '' if mode == 'なし' else f'＋売りパターン除外（{mode}）'
        families[f'スクリーニングLONG{label}（スコア下限×ATR倍率を毎回選ぶ）'] = long_family(mode)
    families['スクリーニングLONG（除外の有無もスコア下限×ATR倍率と一緒に毎回選ぶ）'] = long_family(None)
    families['スクリーニングSHORT（ATR倍率を毎回選ぶ）'] = lambda s: score_family_series(s)[1]
    for name, (side, cfg) in fixed.items():
        families[name + '（ATR倍率を毎回選ぶ）'] = (lambda c: (lambda s: c))(cfg)

    # 本番設定（スコア75・ATR×3.0）を固定で運用した場合（設定の選び直しなし。ただし精度は区間ごとに測り直す）
    def prod_family(mode):
        def fn(s):
            key = (mode, PROD['threshold'], PROD['atr'])
            return {key: score_family_series(s)[0][key]}
        return fn
    prod_name = f'本番設定 固定（スコア{PROD["threshold"]}点以上・ATR×{PROD["atr"]}）'
    for mode in EXCLUDE_LOOKBACK:
        label = '' if mode == 'なし' else f'＋売りパターン除外（{mode}）'
        families[prod_name + label] = prod_family(mode)

    # 対になる比較（除外あり − 除外なし）：同じ日の成績の差を検定する
    pairs = []
    for mode in EXCLUDE_LOOKBACK:
        if mode == 'なし':
            continue
        pairs.append((prod_name + f'＋売りパターン除外（{mode}）', prod_name))
        pairs.append((f'スクリーニングLONG＋売りパターン除外（{mode}）（スコア下限×ATR倍率を毎回選ぶ）',
                      'スクリーニングLONG（スコア下限×ATR倍率を毎回選ぶ）'))
    pairs.append(('スクリーニングLONG（除外の有無もスコア下限×ATR倍率と一緒に毎回選ぶ）',
                  'スクリーニングLONG（スコア下限×ATR倍率を毎回選ぶ）'))

    oos_idx = v.stationary_bootstrap_indices(n_dates - oos_lo, N_BOOT, MEAN_BLOCK)
    report = {'families': {}, 'paired': {}, 'folds': [[dates[s], dates[e - 1]] for s, e in folds]}
    for cost in COSTS:
        rows = []
        oos_by_name = {}
        for name, fn in families.items():
            oos, picks = run_family_wf(name, folds, first_k, n_dates, fn, cost)
            oos_by_name[name] = oos
            ex = oos['excess'][oos_lo:]
            test = v.bootstrap_test(ex, oos_idx)
            summ = v.summarize(ex, active=oos['count'][oos_lo:] > 0)
            raw = v.summarize(oos['raw'][oos_lo:])
            per_fold = []
            for (s, e), pk in zip(folds, picks):
                fs = v.summarize(oos['excess'][s:e])
                per_fold.append({'period': [dates[s], dates[e - 1]],
                                 'picked': None if pk['picked'] is None else str(pk['picked']),
                                 'excess_total_pct': fs['total_pct']})
            rows.append({'name': name, 'excess': summ, 'raw': raw, 'test': test, 'folds': per_fold})
        p = [r['test']['p_value'] for r in rows]
        for r, h, q in zip(rows, v.holm(p), v.benjamini_hochberg(p)):
            r['test']['p_holm'] = round(float(h), 4)
            r['test']['q_bh'] = round(float(q), 4)
        report['families'][str(cost)] = rows
        prs = []
        for a, b in pairs:
            diff = oos_by_name[a]['excess'][oos_lo:] - oos_by_name[b]['excess'][oos_lo:]
            t = v.bootstrap_test(diff, oos_idx)
            per_fold = [round(float(np.sum(oos_by_name[a]['excess'][s:e] - oos_by_name[b]['excess'][s:e])) * 100, 2)
                        for s, e in folds]
            prs.append({'a': a, 'b': b, 'diff': v.summarize(diff), 'test': t, 'folds_total_pct': per_fold,
                        'folds_better': int(sum(1 for x in per_fold if x > 0))})
        report['paired'][str(cost)] = prs
        print(f'[wf] コスト{cost}% の walk-forward 完了', flush=True)

    # --- Reality Check：全期間・全設定（前半で精度を測った場合＝従来の検証と同じ条件）で一番良いものは偶然か ---
    mid = first_k + (n_dates - first_k) // 2
    long_cfg, short_cfg, _ = score_family_series(mid)
    all_cfg = {}
    for (mode, thr, m), s in long_cfg.items():
        label = '' if mode == 'なし' else f'＋除外（{mode}）'
        all_cfg[f'スクリーニングLONG{label} スコア{thr}・ATR×{m}'] = s
    for m, s in short_cfg.items():
        all_cfg[f'スクリーニングSHORT ATR×{m}'] = s
    for name, (_, cfg) in fixed.items():
        for m, s in cfg.items():
            all_cfg[f'{name} ATR×{m}'] = s
    names = sorted(all_cfg)
    X = np.array([all_cfg[n][0.1]['excess'][first_k:] for n in names])
    rc_idx = v.stationary_bootstrap_indices(X.shape[1], N_BOOT, MEAN_BLOCK)
    t_obs, adj, p_best = v.max_t_adjusted(X, rc_idx)
    order = np.argsort(-t_obs)
    report['reality_check'] = {
        'n_configs': len(names), 'p_best': round(p_best, 4),
        'top': [{'name': names[i], 't': round(float(t_obs[i]), 2), 'p_adjusted': round(float(adj[i]), 4),
                 'summary': v.summarize(X[i])} for i in order[:20]],
    }
    print(f'[wf] Reality Check: {len(names)}設定の中の最良 {names[order[0]]} t={t_obs[order[0]]:.2f} 補正p={p_best:.3f}',
          flush=True)

    params = {'train_min_days': TRAIN_MIN, 'test_len_days': TEST_LEN, 'min_entries_train': MIN_ENTRIES_TRAIN,
              'costs_pct_round_trip': COSTS, 'n_boot': N_BOOT, 'mean_block_days': MEAN_BLOCK, 'seed': 20261111,
              'atr_grid': es.ATR_GRID, 'long_score_thresholds': es.LONG_SCORE_THRESHOLDS,
              'long_top_n': es.LONG_TOP_N, 'short_top_n': es.SHORT_TOP_N, 'prod': PROD,
              'exclude_bearish_lookback': EXCLUDE_LOOKBACK,
              'selection_metric': 'train excess IR'}
    report['meta'] = repro.run_metadata([t for t, _ in universe], params,
                                        ['evaluate_walkforward.py', 'validation.py', 'evaluate_strategy.py',
                                         'sakata.py', 'fast_features.py', 'universe_filters.py', 'scoring.py'])
    report['period'] = [dates[first_k], dates[-1]]
    report['oos_period'] = [dates[oos_lo], dates[-1]]
    report['tickers'] = len(data)
    os.makedirs(os.path.dirname(args.out) or '.', exist_ok=True)
    with open(args.out + '.json', 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    with open(args.out + '.md', 'w', encoding='utf-8') as f:
        f.write(render_markdown(report))
    print(f'[wf] 完了: {args.out}.json / .md', flush=True)


def _p(x, d=2):
    return '—' if x is None else f'{x:+.{d}f}%'


def render_markdown(rep):
    meta = rep.get('meta', {})
    code = meta.get('code') or {}
    L = ['# walk-forward 検証と有意性の確認（先読みなし）', '',
         f"- 期間: {rep['period'][0]}〜{rep['period'][1]}（{rep['tickers']}銘柄）。"
         f"walk-forward の成績は {rep['oos_period'][0]}〜 の部分（各区間の前のデータだけで設定を選んだ結果をつなげたもの）",
         '- 区間: ' + ' / '.join(f'{a}〜{b}' for a, b in rep['folds']),
         f"- 設定の選び方: 区間より前の「市場との差」の日次系列で、年率IR（平均÷ばらつき）が最大のもの"
         f"（学習期間の約定{MIN_ENTRIES_TRAIN}件以上）。酒田五法の精度も区間より前の検出だけで測り直す。",
         '- 成績は日次: 保有中の全ポジションの等金額平均。市場との差＝取引可能な全銘柄の等金額平均との差。ポジションなしの日は0。',
         f'- p値: 定常ブートストラップ（平均{MEAN_BLOCK}日のかたまり・{N_BOOT}回）。'
         'Holm＝戦略数ぶんの補正（厳しめ）、BH＝偽陽性の割合を抑える補正（ゆるめ）。0.05未満なら「偶然とは言いにくい」。',
         f"- 再現性: データ指紋 {meta.get('data_fingerprint_sha256', '')[:16]} / コード {str(code.get('git_commit'))[:8]}"
         f"{'（未コミットの変更あり）' if code.get('git_dirty') else ''}", '']
    for cost, rows in rep['families'].items():
        L += [f'## walk-forward の成績（往復コスト {cost}%）', '',
              '| 戦略 | 市場との差 年率 | IR | 最大DD | 95%信頼区間（日次） | p値 | Holm | BH | 単純リターン 年率 | 保有日数 |',
              '|---|---:|---:|---:|---|---:|---:|---:|---:|---:|']
        for r in sorted(rows, key=lambda r: r['test']['p_value']):
            ex, t = r['excess'], r['test']
            ci = t['ci95_daily_pct']
            L.append(f"| {r['name']} | {_p(ex['annual_pct'])} | {ex['ir_annual'] if ex['ir_annual'] is not None else '—'} | "
                     f"{_p(ex['max_drawdown_pct'])} | {ci[0]:+.3f}〜{ci[1]:+.3f}% | {t['p_value']:.3f} | "
                     f"{t['p_holm']:.3f} | {t['q_bh']:.3f} | {_p(r['raw']['annual_pct'])} | {ex['active_days']}/{ex['days']} |")
        L.append('')
    base = rep['families'][str(COSTS[0])]
    L += ['## 区間ごとの「市場との差」（コスト0.1%）と選ばれた設定', '',
          '| 戦略 | ' + ' | '.join(f'{a[5:]}〜' for a, _ in rep['folds']) + ' |',
          '|---|' + '---:|' * len(rep['folds'])]
    for r in sorted(base, key=lambda r: r['test']['p_value']):
        L.append(f"| {r['name']} | " + ' | '.join(
            f"{_p(f['excess_total_pct'], 1)} ({f['picked'] or '見送り'})" for f in r['folds']) + ' |')
    L += ['', '## 売りパターン除外の効果（除外あり − 除外なし、同じ日の「市場との差」の差）', '',
          '差がプラスなら除外したほうが良い。p値は「本当は差がない（またはマイナス）」のにこれだけの差が偶然出る確率。', '']
    for cost, prs in rep['paired'].items():
        L += [f'### 往復コスト {cost}%', '',
              '| 比較 | 差 年率 | 95%信頼区間（日次） | p値 | 除外が勝った区間 | 区間ごとの差 |', '|---|---:|---|---:|---:|---|']
        for r in prs:
            ci = r['test']['ci95_daily_pct']
            L.append(f"| {r['a']} − {r['b'].split('（')[0]} | {_p(r['diff']['annual_pct'])} | {ci[0]:+.3f}〜{ci[1]:+.3f}% | "
                     f"{r['test']['p_value']:.3f} | {r['folds_better']}/{len(r['folds_total_pct'])} | "
                     + ' / '.join(f'{x:+.1f}%' for x in r['folds_total_pct']) + ' |')
        L.append('')
    rc = rep['reality_check']
    L += ['', f"## Reality Check：全期間で{rc['n_configs']}通りの設定を試した中の上位（コスト0.1%）", '',
          f"一番良く見える設定でも、偶然これ以上になる確率（多重比較の補正済み）は **{rc['p_best']:.3f}**。", '',
          '| 設定 | 市場との差 年率 | IR | t値 | 補正p値 |', '|---|---:|---:|---:|---:|']
    for r in rc['top']:
        L.append(f"| {r['name']} | {_p(r['summary']['annual_pct'])} | {r['summary']['ir_annual']} | {r['t']} | {r['p_adjusted']:.3f} |")
    return '\n'.join(L) + '\n'


if __name__ == '__main__':
    main()
