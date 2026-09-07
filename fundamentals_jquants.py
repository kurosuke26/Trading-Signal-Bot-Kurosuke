#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fundamentals_jquants.py — J-Quantsキャッシュ（data/jquants_cache/<code>/fins.json）から
PER/PBR分離・ROE・自己資本比率・成長率などを算出し、`fundamental_snapshot`ブロックを組み立てる
（設計書 `2026-09-07-investor-lens-phase2-design.md` §3・§4に基づく実装）。

追加のAPI呼び出しは一切行わない（既にバックフィル済みのローカルキャッシュのみを読む）ため、
collector.py の日次実行に組み込んでもJ-Quants側のレート制限には影響しない。

【フィールド設計の要点】
- PER = 現在株価 ÷ EPS。EPSは`FEPS`（今期会社予想）を優先し、無ければ直近実績`EPS`に
  フォールバックする（実績EPSは開示時点までの累計値であり満期換算ではない点に注意。
  設計書どおりの割り切り）。
- PBR = 現在株価 ÷ BPS。BPSはFYレコードにのみ存在するため、直近のFYレコードの値を使う
  （＝三菱四半期ごとに動くPBRではなく「最新の期末純資産ベース」のPBR）。
- 連結（Consolidated）優先、値が空の場合のみ非連結（NC接頭辞）にフォールバックする。
- 各項目はデータが無ければNoneを返す（クラッシュさせず「データ不足」として扱う）。
"""

import json
import os

CACHE_DIR = os.getenv('JQUANTS_CACHE_DIR') or 'data/jquants_cache'


def _ticker_to_code4(ticker):
    """'7203.T' -> '7203'。既に4桁コードならそのまま返す。"""
    return str(ticker).split('.')[0]


def load_fins_records(ticker4, cache_dir=None):
    path = os.path.join(cache_dir or CACHE_DIR, ticker4, 'fins.json')
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding='utf-8') as f:
            records = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(records, list):
        return []
    return sorted(records, key=lambda r: r.get('DiscDate') or '')


def parse_num(record, *keys):
    """keysを順に試し、空でない最初の値をfloatにして返す（連結値→非連結値のフォールバック用）。"""
    for key in keys:
        raw = record.get(key)
        if raw is None or raw == '':
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def latest_fy_record(records):
    fy_records = [r for r in records if r.get('CurPerType') == 'FY']
    return fy_records[-1] if fy_records else None


def _year_earlier_match(records, target):
    """targetと同じCurPerType・1年前のCurFYStを持つ開示を探す（YoY比較用）。"""
    target_per_type = target.get('CurPerType')
    target_fy_st = target.get('CurFYSt') or ''
    if not target_per_type or len(target_fy_st) < 4:
        return None
    try:
        prior_year = int(target_fy_st[:4]) - 1
    except ValueError:
        return None
    prior_fy_prefix = f'{prior_year}{target_fy_st[4:]}'
    for r in records:
        if r.get('CurPerType') == target_per_type and r.get('CurFYSt') == prior_fy_prefix:
            return r
    return None


def _growth_pct(current, prior):
    if current is None or prior is None or prior == 0:
        return None
    return round((current - prior) / abs(prior) * 100, 1)


def _sales_growth_3y_avg(records):
    """直近FYと3期前FYのSales成長率（年平均、CAGR）。Freeプランの取得可能期間
    （直近12週間前〜2年12週間前）では3期分のFYが揃わないことが多く、その場合はNoneを返す。"""
    fy_records = [r for r in records if r.get('CurPerType') == 'FY']
    if len(fy_records) < 4:
        return None
    latest = fy_records[-1]
    base = fy_records[-4]
    latest_sales = parse_num(latest, 'Sales', 'NCSales')
    base_sales = parse_num(base, 'Sales', 'NCSales')
    if not latest_sales or not base_sales or base_sales <= 0:
        return None
    cagr = (latest_sales / base_sales) ** (1 / 3) - 1
    return round(cagr * 100, 1)


def _eps_trend(records):
    """直近3開示の実績EPSの向き（up_3q/down_3q/flat_3q）。3件揃わなければNone。"""
    eps_series = []
    for r in records[-6:]:
        val = parse_num(r, 'EPS', 'NCEPS')
        if val is not None:
            eps_series.append(val)
    if len(eps_series) < 3:
        return None
    last3 = eps_series[-3:]
    if last3[0] < last3[1] < last3[2]:
        return 'up_3q'
    if last3[0] > last3[1] > last3[2]:
        return 'down_3q'
    return 'flat_3q'


def build_fundamental_snapshot(ticker, current_price, sector_code=None, sector_name=None,
                                dividend_yield=None, cache_dir=None):
    """
    1銘柄分の`fundamental_snapshot`を組み立てる。J-Quantsキャッシュが無い／financial
    データが無い銘柄では、取得できた項目だけを埋めたdictを返す（全項目Noneでも可、
    呼び出し側のレンズ関数が個別に「データ不足」判定する設計のため例外は投げない）。

    dividend_yieldは既存のyfinance経由の値（collector.pyのfund['dividend_yield']）を
    そのまま受け取って埋め込む。J-Quantsの開示データから独自に再計算はしない
    （既存の判定ロジック・レンズが参照する値と食い違わないようにするため）。
    """
    ticker4 = _ticker_to_code4(ticker)
    records = load_fins_records(ticker4, cache_dir=cache_dir)

    snapshot = {
        'per': None, 'pbr': None, 'roe': None, 'equity_ratio': None,
        'dividend_yield': dividend_yield if dividend_yield else None, 'payout_ratio': None,
        'sales_growth_yoy': None, 'sales_growth_3y_avg': None, 'op_growth_yoy': None,
        'eps_trend': None,
        'sector_code': sector_code, 'sector_name': sector_name,
        'data_asof_doctype': None,
    }
    if not records:
        return snapshot

    latest = records[-1]
    fy = latest_fy_record(records)
    snapshot['data_asof_doctype'] = latest.get('DocType')

    eps = parse_num(latest, 'FEPS', 'EPS', 'NCEPS')
    if eps and current_price:
        snapshot['per'] = round(current_price / eps, 1) if eps > 0 else None

    if fy is not None:
        bps = parse_num(fy, 'BPS', 'NCBPS')
        if bps and current_price:
            snapshot['pbr'] = round(current_price / bps, 2) if bps > 0 else None
        roe = parse_num(fy, 'ROE', 'NCROE')
        if roe is not None:
            snapshot['roe'] = round(roe * 100, 1)
        payout = parse_num(fy, 'PayoutRatioAnn', 'FPayoutRatioAnn')
        if payout is not None:
            snapshot['payout_ratio'] = round(payout * 100, 1)

    equity_ratio = parse_num(latest, 'EqAR', 'NCEqAR')
    if equity_ratio is not None:
        snapshot['equity_ratio'] = round(equity_ratio * 100, 1)

    yoy_ref = _year_earlier_match(records, latest)
    if yoy_ref is not None:
        latest_sales = parse_num(latest, 'Sales', 'NCSales')
        prior_sales = parse_num(yoy_ref, 'Sales', 'NCSales')
        snapshot['sales_growth_yoy'] = _growth_pct(latest_sales, prior_sales)

        latest_op = parse_num(latest, 'OP', 'NCOP')
        prior_op = parse_num(yoy_ref, 'OP', 'NCOP')
        snapshot['op_growth_yoy'] = _growth_pct(latest_op, prior_op)

    snapshot['sales_growth_3y_avg'] = _sales_growth_3y_avg(records)
    snapshot['eps_trend'] = _eps_trend(records)

    return snapshot
