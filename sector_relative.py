#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sector_relative.py — 同じ業種の中での相対評価（2026-09-16追加）。

【背景】先読みなしの検証で、本番LONG（複合スコア）は市場平均は上回るものの、同じ業種の平均には
届いていなかった（後半期間：市場との差+1.4%に対し、業種との差−0.5%）。つまり「強い業種を選べていた」
効果が大きく、業種の中では平均並みだった。そこで、同じ日・同じ業種の中での位置を計算して候補選定に使う。

【計算する2つの相対指標】（すべてその日の引けまでの情報だけ。未来の値は使わない）
- 相対モメンタム：その銘柄の60営業日リターン −（同業種の銘柄の60営業日リターンの平均）
- 相対割安：その銘柄のPER×PBR ÷（同業種のPER×PBRの中央値）。1未満なら業種内で割安

業種は universe.py がJPX一覧から取得した33業種区分（collector.py が fund['sector_name'] に入れている）。
同じ業種の対象が MIN_SECTOR_MEMBERS 未満の日は、相対評価を「判定不能（None）」とし、条件には使わない。
"""

MIN_SECTOR_MEMBERS = 5
RET_WINDOW = 60


def _ret_window(df, window=RET_WINDOW):
    if df is None or len(df) < window + 1 or 'Close' not in df:
        return None
    past, now = df['Close'].iloc[-(window + 1)], df['Close'].iloc[-1]
    if not (past and past > 0 and now == now):
        return None
    return float(now / past - 1)


def _median(values):
    vs = sorted(values)
    n = len(vs)
    if not n:
        return None
    return vs[n // 2] if n % 2 else (vs[n // 2 - 1] + vs[n // 2]) / 2


def annotate(results, histories, min_members=MIN_SECTOR_MEMBERS):
    """
    results（collector.analyze_ticker の結果 dict）に 'sector_relative' を追記する。

    sector_relative = {
      'sector': 業種名, 'ret60': 自分の60日リターン, 'sector_ret60': 同業種の平均,
      'rel_ret60': 差（プラスなら業種内で強い）, 'sector_median_per_pbr': 同業種の中央値,
      'per_pbr_vs_sector': 自分÷中央値（1未満なら業種内で割安）,
      'momentum_ok': bool or None, 'value_ok': bool or None,
    }
    対象が少ない業種・データ不足の銘柄は None のままにする（条件に使わない）。
    """
    by_sector_ret, by_sector_pp = {}, {}
    ret60 = {}
    for ticker, r in results.items():
        sector = (r.get('fundamental_snapshot') or {}).get('sector_name') or r.get('sector_name')
        if not sector:
            continue
        r['_sector'] = sector
        v = _ret_window(histories.get(ticker))
        if v is not None:
            ret60[ticker] = v
            by_sector_ret.setdefault(sector, []).append(v)
        pp = r.get('per_pbr')
        if pp is not None and pp > 0:
            by_sector_pp.setdefault(sector, []).append(pp)

    sector_ret = {s: sum(v) / len(v) for s, v in by_sector_ret.items() if len(v) >= min_members}
    sector_pp = {s: _median(v) for s, v in by_sector_pp.items() if len(v) >= min_members}

    for ticker, r in results.items():
        sector = r.pop('_sector', None)
        if not sector:
            r['sector_relative'] = None
            continue
        sr = sector_ret.get(sector)
        mp = sector_pp.get(sector)
        mine_ret, mine_pp = ret60.get(ticker), r.get('per_pbr')
        rel_ret = (mine_ret - sr) if (mine_ret is not None and sr is not None) else None
        ratio = (mine_pp / mp) if (mine_pp and mp and mp > 0) else None
        r['sector_relative'] = {
            'sector': sector,
            'ret60': round(mine_ret, 4) if mine_ret is not None else None,
            'sector_ret60': round(sr, 4) if sr is not None else None,
            'rel_ret60': round(rel_ret, 4) if rel_ret is not None else None,
            'sector_median_per_pbr': round(mp, 2) if mp is not None else None,
            'per_pbr_vs_sector': round(ratio, 2) if ratio is not None else None,
            'momentum_ok': (rel_ret >= 0) if rel_ret is not None else None,
            'value_ok': (ratio <= 1.0) if ratio is not None else None,
        }
    return results


def passes(r, require_momentum=True, require_value=False):
    """候補選定に使う判定。判定不能（データ不足・小さな業種）は通す（機会を落とさないため）。"""
    sr = r.get('sector_relative') or {}
    if require_momentum and sr.get('momentum_ok') is False:
        return False
    if require_value and sr.get('value_ok') is False:
        return False
    return True


def format_line(r):
    """Discord投稿用の1行。相対評価が無ければ空文字。"""
    sr = r.get('sector_relative') or {}
    if not sr.get('sector'):
        return ''
    parts = [f"業種：{sr['sector']}"]
    if sr.get('rel_ret60') is not None:
        parts.append(f"60日リターンの業種平均との差 {sr['rel_ret60'] * 100:+.1f}%")
    if sr.get('per_pbr_vs_sector') is not None:
        parts.append(f"PER×PBRは業種中央値の{sr['per_pbr_vs_sector']:.2f}倍")
    return ' ／ '.join(parts)
