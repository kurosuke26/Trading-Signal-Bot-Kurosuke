#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dividend_hikes.py — 「増配のお知らせ」を適時開示（TDnet）から拾う（2026-10-29追加）。

breaking_alerts.py（毎日の「今日の増配」）と weekly_tips.py（週間の「今週の増配」）から
共通で使う。discord-ai-team側のdisclosure_fetcher.pyと同じ非公式ミラーAPI
「東証TDnet WEB-API by やのしん」(https://webapi.yanoshin.jp/tdnet/) を使う。

【判定の考え方（正直な制約）】
- 開示タイトルに「増配」と明記されているものだけを対象にする（例：「配当予想の修正（増配）
  に関するお知らせ」）。「配当予想の修正」とだけ書かれた開示は、タイトルだけでは増配か
  減配か分からないため対象外にしている（取りこぼしはあり得る）。
- 「減配」「無配」を含むタイトルは除外する。
- 同じ会社が同じ期間に複数の開示を出している場合は、1社1件にまとめる。
- 取得元は非公式のミラーのため、取得できない日もある。その場合は空を返し、呼び出し側は
  「取得できなかった」ことを正直に扱う（Noneと空リストを区別する）。
"""

import json
import os
from datetime import date

import requests

TDNET_API_BASE = 'https://webapi.yanoshin.jp/webapi/tdnet/list'
REQUEST_TIMEOUT_SEC = 25
SNAPSHOT_PATH = os.getenv('SCAN_OUTPUT_PATH') or 'data/latest_scan.json'
FETCH_LIMIT = 3000  # 1週間分でも800件前後なので十分
MIRROR_REDIRECT_PREFIX = 'https://webapi.yanoshin.jp/rd.php?'


def fetch_disclosures(start, end=None):
    """start〜end（date）の適時開示を返す。取得失敗時はNone（0件の空リストとは区別する）。"""
    end = end or start
    span = f'{start:%Y%m%d}' if start == end else f'{start:%Y%m%d}-{end:%Y%m%d}'
    url = f'{TDNET_API_BASE}/{span}.json'
    try:
        resp = requests.get(url, params={'limit': FETCH_LIMIT}, timeout=REQUEST_TIMEOUT_SEC,
                            headers={'User-Agent': 'Mozilla/5.0 (KurosukeBot dividend_hikes.py)'})
        resp.raise_for_status()
        items = resp.json().get('items', [])
    except Exception as e:
        print(f'[dividend_hikes] 適時開示の取得に失敗: {e}')
        return None
    return [i['Tdnet'] for i in items if isinstance(i, dict) and 'Tdnet' in i]


def is_dividend_hike(title):
    return bool(title) and '増配' in title and not any(k in title for k in ('減配', '無配'))


def code4(company_code):
    """TDnetの5桁コード（末尾に0が付く）を、通常の4桁コードに直す。例：72760→7276、219A0→219A"""
    return (company_code or '')[:4]


def extract_hikes(disclosures):
    """増配の開示を、1社1件・新しい順で返す。"""
    seen = set()
    hikes = []
    for d in sorted(disclosures, key=lambda x: x.get('pubdate', ''), reverse=True):
        if not is_dividend_hike(d.get('title', '')):
            continue
        code = code4(d.get('company_code'))
        if not code or code in seen:
            continue
        seen.add(code)
        hikes.append(d)
    return hikes


def load_long_candidates(path=None):
    """最新のスキャン結果から「割安かも」(LONG)判定の銘柄を {4桁コード: スコア} で返す。読めなければ空。"""
    path = path or SNAPSHOT_PATH
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding='utf-8') as f:
            results = json.load(f).get('results', {})
    except Exception:
        return {}
    out = {}
    for ticker, r in results.items():
        if r.get('signal') == 'LONG':
            out[ticker.split('.')[0]] = r.get('score')
    return out


def _tags(title):
    tags = []
    if '中間配当' in title:
        tags.append('中間')
    if '期末配当' in title:
        tags.append('期末')
    if '上方修正' in title:
        tags.append('業績上方修正も')
    return tags


def format_hike_line(d, long_map):
    code = code4(d.get('company_code'))
    name = d.get('company_name') or code
    label = f'{name}（{code}）'
    url = (d.get('document_url') or '').replace(MIRROR_REDIRECT_PREFIX, '')  # 中継URLを外し、TDnetのPDFへ直接リンク
    head = f'[{label}]({url})' if url else label
    pub = d.get('pubdate', '')[5:10].replace('-', '/')
    tags = _tags(d.get('title', ''))
    line = f'・{head} {pub}'
    if tags:
        line += ' ' + '・'.join(tags)
    if code in long_map:
        score = long_map[code]
        line += f' 🎯割安かも{score:.0f}点' if score is not None else ' 🎯割安かも'
    return line


def sort_for_display(hikes, long_map):
    """「割安かも」と重なる銘柄を先に、あとは新しい順（元の並びを保つ）。"""
    return sorted(hikes, key=lambda d: 0 if code4(d.get('company_code')) in long_map else 1)


def build_hike_lines(hikes, long_map, max_items):
    """表示用の行（最大max_items社）と、省いた社数を返す。"""
    ordered = sort_for_display(hikes, long_map)
    lines = [format_hike_line(d, long_map) for d in ordered[:max_items]]
    return lines, max(0, len(ordered) - max_items)


HIKE_EXPLAIN = '💡 増配＝会社が株主に払う配当を増やすこと。株価の反応は「予想とのズレ」次第です。'
HIKE_NOTE = '※開示タイトルから機械的に抽出。🎯＝このシステムの「割安かも」でもある銘柄。売買のおすすめではありません。'
