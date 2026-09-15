#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
evaluate_sakata.py — 酒田五法の各パターン・パターンスコア別に「検出後5日・20日の
上昇率と勝率」を、J-Quantsキャッシュ（data/jquants_cache）の全銘柄・全営業日で実測する
分析専用スクリプト（本番コード・trackingロジックには一切手を加えない）。

【測り方】
- 各銘柄・各営業日tについて、t日引け時点までの日足（直近220本）で
  sakata.detect_all_patterns() / sakata_score() を実行する（本番と同じ関数を再利用）。
- エントリーは「翌営業日(t+1)の始値」、エグジットは「t+5 / t+20営業日の終値」。
  シグナルを見てから翌朝に注文する、という実運用に即した保守的な置き方。
- 株価は分割調整済み（AdjO/AdjH/AdjL/AdjC）を使う（本番collector.pyのauto_adjust=Trueと揃える）。
- 勝率：買いパターンは「上昇率>0」、売りパターンは「上昇率<0（＝空売りで勝ち）」を勝ちとする。
- 超過：同じ日にエントリーした全銘柄の平均上昇率を差し引いた値（地合いの影響を除いた実力）。
- 取引可能：universe_filters.tradeable_flags()（売買代金不足・売買不成立日が多い・仕手株化の兆候を除外）を
  検出日時点で満たす銘柄だけに絞った集計も併記する。株価の上限は考慮しない。
- 再現性：銘柄はコード順に処理し、乱数は使わない。出力JSONの meta にコード版・データ指紋・パラメータを記録する。
- 上場廃止：_universe.json（現在の上場銘柄）に加え、backfill_delisted.py で取得した期間中の
  上場廃止銘柄（_delisted.json）も対象にする（生存者バイアス対策）。検出からN営業日以内に
  上場廃止になった場合は、最終取引日の終値で手仕舞いしたものとして成績に含める
  （TOBのプレミアムも、破綻による暴落も、そのまま結果に反映される）。
- 未来の情報は使わない：検出は t日の引けまでの足だけ、エントリーは t+1日の始値。

【実行方法】
  python evaluate_sakata.py                         # 現行の sakata.py を評価
  python evaluate_sakata.py --impl path/to/old.py   # 別実装（旧版など）を評価して比較
  オプション: --max-tickers 300 / --workers 8 / --out data/backtest_out/sakata_eval_current
"""

import argparse
import importlib.util
import json
import math
import os
import sys
from collections import defaultdict
from multiprocessing import Pool

import pandas as pd

import repro
from fast_features import precompute_technicals
from universe_filters import filter_params, tradeable_flags

CACHE_DIR = os.getenv('JQUANTS_CACHE_DIR') or 'data/jquants_cache'
LOOKBACK_ROWS = 220
MIN_ROWS = 80
HORIZONS = (5, 20)
LIQUID_MIN_YEN = 50_000_000  # 直近20日平均売買代金5,000万円
SCORE_BUCKETS = [(0.0, 0.0, '0（検出なし・相殺）'), (0.0001, 0.3999, '0.01〜0.39'),
                 (0.4, 0.5999, '0.40〜0.59'), (0.6, 0.6999, '0.60〜0.69'),
                 (0.7, 0.7999, '0.70〜0.79'), (0.8, 1.0, '0.80〜1.00')]

_impl = None


def _load_impl(path):
    if not path:
        import sakata
        return sakata
    spec = importlib.util.spec_from_file_location('sakata_impl', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _init_worker(impl_path):
    global _impl
    _impl = _load_impl(impl_path)


def load_eval_universe(max_tickers=0):
    """評価対象：現在の上場銘柄＋期間中の上場廃止銘柄。戻り値 [(ticker4, is_delisted)]。"""
    with open(os.path.join(CACHE_DIR, '_universe.json'), encoding='utf-8') as f:
        out = [(u['ticker4'], False) for u in json.load(f)['universe']]
    if max_tickers:
        out = out[:max_tickers]
    delisted_path = os.path.join(CACHE_DIR, '_delisted.json')
    if os.path.exists(delisted_path) and not max_tickers:
        with open(delisted_path, encoding='utf-8') as f:
            listed = {t for t, _ in out}
            for r in json.load(f).get('delisted', []):
                if r['ticker4'] not in listed and os.path.exists(os.path.join(CACHE_DIR, r['ticker4'], 'bars.json')):
                    out.append((r['ticker4'], True))
    return out


def load_adjusted_bars(ticker4, return_raw=False):
    path = os.path.join(CACHE_DIR, ticker4, 'bars.json')
    if not os.path.exists(path):
        return None
    with open(path, encoding='utf-8') as f:
        rows = json.load(f)
    if not rows:
        return None
    df = pd.DataFrame(rows)
    raw = df.copy()
    need = ['AdjO', 'AdjH', 'AdjL', 'AdjC']
    if any(c not in df.columns for c in need):
        return None
    # 生の終値（RawClose）と分割係数（AdjFactor）も残す：1株あたり指標（EPS等）を
    # その日の株数ベースに換算するのに使う（evaluate_strategy.py 参照）
    df['RawClose'] = pd.to_numeric(df.get('C'), errors='coerce')
    df['AdjFactor'] = pd.to_numeric(df.get('AdjFactor', 1.0), errors='coerce').fillna(1.0)
    df = df.rename(columns={'AdjO': 'Open', 'AdjH': 'High', 'AdjL': 'Low', 'AdjC': 'Close',
                            'AdjVo': 'Volume', 'Va': 'Value'})
    df['Date'] = pd.to_datetime(df['Date'])
    df = df.sort_values('Date').set_index('Date')
    for col in ['Open', 'High', 'Low', 'Close', 'Volume', 'Value']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')
    # 分割係数は売買が無かった日にも付くことがあるため、除外する行の係数は次の行へ持ち越す
    valid = df['Open'].notna() & df['High'].notna() & df['Low'].notna() & df['Close'].notna() & \
        (df['Open'] > 0) & (df['Close'] > 0)
    carry = 1.0
    factors = []
    for ok, fac in zip(valid.to_numpy(), df['AdjFactor'].to_numpy()):
        carry *= fac
        if ok:
            factors.append(carry)
            carry = 1.0
    df = df[valid].copy()
    df['AdjFactor'] = factors
    out = df[['Open', 'High', 'Low', 'Close', 'Volume', 'Value', 'RawClose', 'AdjFactor']]
    return (out, raw) if return_raw else out


def _bucket_label(score):
    for lo, hi, label in SCORE_BUCKETS:
        if lo <= score <= hi:
            return label
    return '範囲外'


def evaluate_ticker(item):
    """1銘柄分。検出レコードと、日付別の全銘柄平均（地合い）計算用の合計値を返す。"""
    ticker4, is_delisted = item
    loaded = load_adjusted_bars(ticker4, return_raw=True)
    if loaded is None:
        return None
    df, raw = loaded
    if len(df) < MIN_ROWS + 2:
        return None
    tradeable, _ = tradeable_flags(raw, df, precompute_technicals(df)['atr'])

    opens = df['Open'].values
    closes = df['Close'].values
    values = df['Value'].values if 'Value' in df.columns else None
    dates = [d.strftime('%Y-%m-%d') for d in df.index]
    n = len(df)
    hmax = max(HORIZONS)

    detections = []        # パターン検出1件ごと
    score_records = []     # スコア>0の日（1日1件）
    zero_score = defaultdict(lambda: [0, 0.0, 0, 0.0, 0, 0])  # (date, liq) -> [n5,sum5,n20,sum20,wins5,wins20] スコア0の日
    market = defaultdict(lambda: [0, 0.0, 0.0])  # (date, h) -> [n, sum, liq_flag_dummy]

    # 上場廃止銘柄は、N営業日後が無くても最終取引日の終値で手仕舞いしたものとして評価する
    last_i = n - 2 if is_delisted else n - hmax - 1
    for i in range(MIN_ROWS - 1, last_i):
        entry = opens[i + 1]
        if not entry or entry <= 0:
            continue
        rets = {h: closes[min(i + h, n - 1)] / entry - 1 for h in HORIZONS}
        if any(not math.isfinite(r) for r in rets.values()):
            continue
        liq = bool(tradeable[i])  # 取引可能（流動性・仕手株フィルター通過）

        date = dates[i]
        for h in HORIZONS:
            m = market[(date, h)]
            m[0] += 1
            m[1] += rets[h]

        window = df.iloc[max(0, i - LOOKBACK_ROWS + 1):i + 1]
        patterns = _impl.detect_all_patterns(window)
        score, _ = _impl.sakata_score(patterns)
        for p in patterns:
            if p.get('detected') and p.get('direction') in ('bullish', 'bearish'):
                detections.append({'t': ticker4, 'd': date, 'name': p['name'],
                                   'dir': p['direction'], 'liq': liq,
                                   'r5': rets[5], 'r20': rets[20]})
        if score and score > 0:
            score_records.append({'t': ticker4, 'd': date, 'score': float(score), 'liq': liq,
                                  'r5': rets[5], 'r20': rets[20]})
        else:
            z = zero_score[(date, liq)]
            z[0] += 1
            z[1] += rets[5]
            z[2] += 1
            z[3] += rets[20]
            z[4] += 1 if rets[5] > 0 else 0
            z[5] += 1 if rets[20] > 0 else 0

    return {
        'detections': detections,
        'score_records': score_records,
        'zero_score': {f'{k[0]}|{int(k[1])}': v for k, v in zero_score.items()},
        'market': {f'{k[0]}|{k[1]}': v[:2] for k, v in market.items()},
    }


def _summarize(rows, direction, market_mean):
    """rows: [{'d','r5','r20'}]。direction='bullish'なら上昇で勝ち、'bearish'なら下落で勝ち。"""
    out = {'n': len(rows)}
    if not rows:
        return out
    for h in HORIZONS:
        key = f'r{h}'
        rs = [r[key] for r in rows]
        ex = [r[key] - market_mean.get((r['d'], h), 0.0) for r in rows]
        if direction == 'bearish':
            wins = sum(1 for x in rs if x < 0)
            ex_wins = sum(1 for x in ex if x < 0)
        else:
            wins = sum(1 for x in rs if x > 0)
            ex_wins = sum(1 for x in ex if x > 0)
        mean = sum(rs) / len(rs)
        ex_mean = sum(ex) / len(ex)
        sd = (sum((x - ex_mean) ** 2 for x in ex) / (len(ex) - 1)) ** 0.5 if len(ex) > 1 else 0
        tstat = ex_mean / (sd / len(ex) ** 0.5) if sd > 0 else None
        out[f'{h}d'] = {
            'mean_return_pct': round(mean * 100, 3),
            'win_rate_pct': round(wins / len(rs) * 100, 1),
            'excess_return_pct': round(ex_mean * 100, 3),
            'excess_win_rate_pct': round(ex_wins / len(ex) * 100, 1),
            't_stat_excess': round(tstat, 2) if tstat is not None else None,
        }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--impl', default=None, help='評価する sakata 実装ファイル（省略時は現行 sakata.py）')
    ap.add_argument('--max-tickers', type=int, default=0)
    ap.add_argument('--workers', type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument('--out', default='data/backtest_out/sakata_eval_current')
    args = ap.parse_args()

    universe = load_eval_universe(args.max_tickers)
    n_delisted = sum(1 for _, d in universe if d)
    print(f'[evaluate_sakata] 対象 {len(universe)} 銘柄（うち上場廃止 {n_delisted}） / workers={args.workers} '
          f'/ impl={args.impl or "sakata.py"}', flush=True)

    detections, score_records = [], []
    zero_score = defaultdict(lambda: [0, 0.0, 0, 0.0, 0, 0])
    market_sum = defaultdict(lambda: [0, 0.0])
    universe = sorted(universe)  # 再現性：常に銘柄コード順
    with Pool(args.workers, initializer=_init_worker, initargs=(args.impl,)) as pool:
        for k, res in enumerate(pool.imap(evaluate_ticker, universe, chunksize=4)):
            if (k + 1) % 200 == 0:
                print(f'[evaluate_sakata] {k + 1}/{len(universe)} 銘柄完了', flush=True)
            if not res:
                continue
            detections.extend(res['detections'])
            score_records.extend(res['score_records'])
            for key, v in res['zero_score'].items():
                z = zero_score[key]
                for j in range(6):
                    z[j] += v[j]
            for key, v in res['market'].items():
                m = market_sum[key]
                m[0] += v[0]
                m[1] += v[1]

    market_mean = {}
    for key, (cnt, total) in market_sum.items():
        d, h = key.split('|')
        market_mean[(d, int(h))] = total / cnt if cnt else 0.0
    all_n = sum(v[0] for k, v in market_sum.items() if k.endswith('|5'))
    base = {h: sum(v[1] for k, v in market_sum.items() if k.endswith(f'|{h}')) / max(1, all_n) for h in HORIZONS}

    report = {'impl': args.impl or 'sakata.py', 'tickers': len(universe), 'delisted_tickers': n_delisted,
              'stock_days': all_n,
              'meta': repro.run_metadata([t for t, _ in universe],
                                         {'horizons': HORIZONS, 'lookback_rows': LOOKBACK_ROWS, 'min_rows': MIN_ROWS,
                                          'score_buckets': SCORE_BUCKETS, 'universe_filter': filter_params()},
                                         ['evaluate_sakata.py', args.impl or 'sakata.py', 'universe_filters.py',
                                          'fast_features.py']),
              'baseline_mean_return_pct': {f'{h}d': round(base[h] * 100, 3) for h in HORIZONS},
              'patterns': {}, 'score_buckets': {}}

    for liq_only in (False, True):
        scope = 'liquid' if liq_only else 'all'
        groups = defaultdict(list)
        for r in detections:
            if liq_only and not r['liq']:
                continue
            groups[(r['name'], r['dir'])].append(r)
        report['patterns'][scope] = {
            f'{name}|{direction}': _summarize(rows, direction, market_mean)
            for (name, direction), rows in sorted(groups.items())
        }

        buckets = defaultdict(list)
        for r in score_records:
            if liq_only and not r['liq']:
                continue
            buckets[_bucket_label(r['score'])].append(r)
        report['score_buckets'][scope] = {
            label: _summarize(buckets[label], 'bullish', market_mean)
            for _, _, label in SCORE_BUCKETS[1:] if buckets.get(label)
        }
        zn = zs5 = zs20 = zw5 = zw20 = zm5 = zm20 = 0
        for key, v in zero_score.items():
            d, liq_flag = key.split('|')
            if liq_only and liq_flag != '1':
                continue
            zn += v[0]
            zs5 += v[1]
            zs20 += v[3]
            zw5 += v[4]
            zw20 += v[5]
            zm5 += v[0] * market_mean.get((d, 5), 0.0)
            zm20 += v[0] * market_mean.get((d, 20), 0.0)
        if zn:
            report['score_buckets'][scope][SCORE_BUCKETS[0][2]] = {
                'n': zn,
                '5d': {'mean_return_pct': round(zs5 / zn * 100, 3), 'win_rate_pct': round(zw5 / zn * 100, 1),
                       'excess_return_pct': round((zs5 - zm5) / zn * 100, 3)},
                '20d': {'mean_return_pct': round(zs20 / zn * 100, 3), 'win_rate_pct': round(zw20 / zn * 100, 1),
                        'excess_return_pct': round((zs20 - zm20) / zn * 100, 3)}}

    os.makedirs(os.path.dirname(args.out) or '.', exist_ok=True)
    with open(args.out + '.json', 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    with open(args.out + '.md', 'w', encoding='utf-8') as f:
        f.write(render_markdown(report))
    print(f'[evaluate_sakata] 完了: {args.out}.json / .md', flush=True)


def render_markdown(rep):
    lines = [f"# 酒田五法 実測レポート（{rep['impl']}）", '',
             f"- 対象: {rep['tickers']}銘柄（うち期間中の上場廃止 {rep.get('delisted_tickers', 0)}） / 評価した銘柄×日: {rep['stock_days']:,}",
             '- 検出は各日の引けまでの足だけで判定（未来の株価は使わない）。上場廃止は最終取引日の終値で手仕舞い。',
             f"- 再現性: データ指紋 {rep.get('meta', {}).get('data_fingerprint_sha256', '')[:16]} / "
             f"コード {str((rep.get('meta', {}).get('code') or {}).get('git_commit'))[:8]}"
             f"{'（未コミットの変更あり）' if (rep.get('meta', {}).get('code') or {}).get('git_dirty') else ''}",
             f"- 全銘柄×全日の平均上昇率（比較の基準）: 5日 {rep['baseline_mean_return_pct']['5d']}% / "
             f"20日 {rep['baseline_mean_return_pct']['20d']}%",
             '- エントリー＝検出翌営業日の始値、エグジット＝5/20営業日後の終値。売りパターンの勝率は「下落した割合」。',
             '- 超過＝同日エントリーの全銘柄平均との差（地合いを除いた値）。',
             '- **実力**＝超過を「当たる向き」にそろえた値（売りパターンは符号を反転）。プラスなら地合い以上に当たっている。',
             '- t値は同じ日に検出が集中する影響を考慮していないため、目安（±2〜3を超えると偶然とは考えにくい）として見ること。', '']
    for scope, title in (('all', '全銘柄'), ('liquid', '取引可能な銘柄のみ（売買代金不足・仕手株化を除外）')):
        lines += [f'## パターン別（{title}）', '',
                  '| パターン | 方向 | 件数 | 検出頻度 | 5日 上昇率 | 5日 勝率 | 5日 実力 | 20日 上昇率 | 20日 勝率 | 20日 実力 | 20日 t値 |',
                  '|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
        for key, s in rep['patterns'][scope].items():
            name, direction = key.split('|')
            if s['n'] == 0:
                continue
            a, b = s['5d'], s['20d']
            sign = 1 if direction == 'bullish' else -1
            freq = s['n'] / max(1, rep['stock_days']) * 100
            t = b['t_stat_excess']
            lines.append(f"| {name} | {'買い' if direction == 'bullish' else '売り'} | {s['n']:,} | {freq:.2f}% | "
                         f"{a['mean_return_pct']:+.2f}% | {a['win_rate_pct']:.1f}% | {sign * a['excess_return_pct']:+.2f}% | "
                         f"{b['mean_return_pct']:+.2f}% | {b['win_rate_pct']:.1f}% | {sign * b['excess_return_pct']:+.2f}% | "
                         f"{'' if t is None else f'{sign * t:+.1f}'} |")
        lines += ['', f'## パターンスコア別（{title}）', '',
                  '| スコア帯 | 件数 | 5日 上昇率 | 5日 勝率 | 5日 超過 | 20日 上昇率 | 20日 勝率 | 20日 超過 |',
                  '|---|---:|---:|---:|---:|---:|---:|---:|']
        for _, _, label in SCORE_BUCKETS:
            s = rep['score_buckets'][scope].get(label)
            if not s:
                continue
            a, b = s['5d'], s['20d']
            lines.append(f"| {label} | {s['n']:,} | {a['mean_return_pct']:+.2f}% | {a['win_rate_pct']:.1f}% | "
                         f"{a.get('excess_return_pct', float('nan')):+.2f}% | {b['mean_return_pct']:+.2f}% | "
                         f"{b.get('win_rate_pct', float('nan')):.1f}% | {b.get('excess_return_pct', float('nan')):+.2f}% |")
        lines.append('')
    return '\n'.join(lines)


if __name__ == '__main__':
    sys.exit(main())
