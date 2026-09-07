#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
compute_sector_stats.py — 東証33業種ごとのPER/PBR/ROE平均値を、既にバックフィル済みの
J-Quantsキャッシュ（data/jquants_cache/、約3,735銘柄）全体から計算し、
data/sector_stats.json にキャッシュする（設計書 §6「セクター連動派の扱い」に基づく実装）。

【なぜ候補20銘柄からではなく全キャッシュから計算するか】
data/latest_scan.json のLONG/SHORT候補（20銘柄程度）だけでは、同じ業種の銘柄が
複数含まれる保証がなく、業種平均としてのサンプル数が不足する。既にバックフィル済みの
全銘柄キャッシュを使うことで、業種ごとに統計的に意味のある平均値を計算する。

【価格データについて】
J-Quants Freeプランの取得可能期間は「直近12週間前〜2年12週間前」の固定窓のため、
bars.jsonの最新レコードでも実際には約3ヶ月前の終値になる。当日の株価取得（yfinance）を
このバッチのために追加で行うことはせず（対象が3,000銘柄超のため往復コストが大きい）、
「業種内の相対的な位置づけ」を見る目的には十分な精度と割り切り、bars.jsonの直近終値を
PER/PBR計算の株価として使う。

実行方法（週次など、頻繁な更新が不要なためcollector.pyの日次実行とは別に想定）:
    python compute_sector_stats.py
"""

import json
import os
import sys
from datetime import datetime, timezone

from fundamentals_jquants import load_fins_records, latest_fy_record, parse_num, CACHE_DIR
from universe import get_all_tse_tickers, FALLBACK_TICKERS

OUT_PATH = os.getenv('SECTOR_STATS_PATH') or 'data/sector_stats.json'


def _latest_close(ticker4, cache_dir=None):
    bars_path = os.path.join(cache_dir or CACHE_DIR, ticker4, 'bars.json')
    if not os.path.exists(bars_path):
        return None
    try:
        with open(bars_path, encoding='utf-8') as f:
            bars = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    if not bars:
        return None
    bars = sorted(bars, key=lambda b: b.get('Date') or '')
    last = bars[-1]
    close = last.get('AdjC') or last.get('C')
    try:
        return float(close) if close is not None else None
    except (TypeError, ValueError):
        return None


def _per_pbr_roe_for_ticker(ticker4):
    records = load_fins_records(ticker4)
    if not records:
        return None, None, None

    current_price = _latest_close(ticker4)
    latest = records[-1]
    fy = latest_fy_record(records)

    per = None
    eps = parse_num(latest, 'FEPS', 'EPS', 'NCEPS')
    if eps and current_price and eps > 0:
        per = current_price / eps

    pbr = None
    roe = None
    if fy is not None:
        bps = parse_num(fy, 'BPS', 'NCBPS')
        if bps and current_price and bps > 0:
            pbr = current_price / bps
        roe_raw = parse_num(fy, 'ROE', 'NCROE')
        if roe_raw is not None:
            roe = roe_raw * 100

    return per, pbr, roe


def resolve_sector_by_code4():
    """universe.pyからコード4桁 -> (sector_code, sector_name) の辞書を作る。"""
    tickers, meta_df = get_all_tse_tickers()
    if meta_df is None:
        print('[compute_sector_stats] JPX銘柄一覧が取得できなかった'
              f'（フォールバック{len(FALLBACK_TICKERS)}銘柄のみ）ため、業種平均は計算できません',
              file=sys.stderr)
        return {}

    out = {}
    for row in meta_df.to_dict('records'):
        code = str(row.get('code') or '').strip()
        sector_code = row.get('sector_code')
        sector_name = row.get('sector_name')
        if not code or sector_code is None or str(sector_code) == 'None':
            continue
        out[code] = (str(sector_code).strip(), sector_name)
    return out


def compute():
    sector_by_code4 = resolve_sector_by_code4()
    if not sector_by_code4:
        print('[compute_sector_stats] 業種コードが1件も取得できなかったため中断します', file=sys.stderr)
        return None

    if not os.path.isdir(CACHE_DIR):
        print(f'[compute_sector_stats] キャッシュディレクトリが見つかりません: {CACHE_DIR}', file=sys.stderr)
        return None

    cached_codes = [d for d in os.listdir(CACHE_DIR) if os.path.isdir(os.path.join(CACHE_DIR, d))]
    print(f'[compute_sector_stats] キャッシュ済み{len(cached_codes)}銘柄を対象に集計します')

    buckets = {}  # sector_code -> {'name':, 'per': [...], 'pbr': [...], 'roe': [...]}
    matched, unmatched = 0, 0
    for code4 in cached_codes:
        sector_info = sector_by_code4.get(code4)
        if sector_info is None:
            unmatched += 1
            continue
        matched += 1
        sector_code, sector_name = sector_info

        per, pbr, roe = _per_pbr_roe_for_ticker(code4)
        bucket = buckets.setdefault(sector_code, {'name': sector_name, 'per': [], 'pbr': [], 'roe': []})
        if per is not None and per > 0:
            bucket['per'].append(per)
        if pbr is not None and pbr > 0:
            bucket['pbr'].append(pbr)
        if roe is not None:
            bucket['roe'].append(roe)

    print(f'[compute_sector_stats] 業種コード突合: {matched}件マッチ / {unmatched}件不明')

    stats = {}
    for sector_code, bucket in buckets.items():
        stats[sector_code] = {
            'sector_name': bucket['name'],
            'per_avg': round(sum(bucket['per']) / len(bucket['per']), 1) if bucket['per'] else None,
            'per_count': len(bucket['per']),
            'pbr_avg': round(sum(bucket['pbr']) / len(bucket['pbr']), 2) if bucket['pbr'] else None,
            'pbr_count': len(bucket['pbr']),
            'roe_avg': round(sum(bucket['roe']) / len(bucket['roe']), 1) if bucket['roe'] else None,
            'roe_count': len(bucket['roe']),
        }

    out = {
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'sector_count': len(stats),
        'sectors': stats,
    }

    out_dir = os.path.dirname(OUT_PATH)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    tmp_path = OUT_PATH + '.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, OUT_PATH)
    print(f'[compute_sector_stats] {len(stats)}業種分を書き込みました: {OUT_PATH}')
    return out


if __name__ == '__main__':
    result = compute()
    sys.exit(0 if result is not None else 1)
