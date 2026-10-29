#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
disclosure_picks.py — 配当・株主優待の「お知らせ」を適時開示（TDnet）から拾う（2026-10-29追加）。

拾う種類：増配／減配・無配／優待の拡充・新設・導入／優待の廃止・終了／優待の変更（方向不明）。
breaking_alerts.py（毎日の「今日の配当・優待のお知らせ」）と weekly_tips.py（週間の
「今週の増配・減配・優待」）から共通で使う。discord-ai-team側のdisclosure_fetcher.pyと
同じ非公式ミラーAPI「東証TDnet WEB-API by やのしん」(https://webapi.yanoshin.jp/tdnet/) を使う。

【判定の考え方（正直な制約）】
- 開示「タイトル」のキーワードだけで機械的に分類する（PDF本文は見ない）。
  「配当予想の修正」とだけ書かれた開示は、増配か減配かがタイトルから分からないため対象外
  （取りこぼしはあり得る）。優待の「一部変更」等も、拡充か改悪かは分からないため
  「変更（方向不明）」として別枠にする。
- 1つの開示が複数の種類に当てはまることがある（例：無配＋優待廃止）。その場合は両方の
  欄に載せる。同じ種類の中では1社1件にまとめる。
- 取得元は非公式のミラーのため、取得できない日もある。その場合はNone（0件の空リストとは
  区別する）を返し、呼び出し側は「取得できなかった」ことを正直に扱う。
"""

import json
import os

import requests

TDNET_API_BASE = 'https://webapi.yanoshin.jp/webapi/tdnet/list'
REQUEST_TIMEOUT_SEC = 25
SNAPSHOT_PATH = os.getenv('SCAN_OUTPUT_PATH') or 'data/latest_scan.json'
FETCH_LIMIT = 3000  # 1週間分でも800件前後なので十分
MIRROR_REDIRECT_PREFIX = 'https://webapi.yanoshin.jp/rd.php?'


# ---------------------------------------------------------------------------
# 取得
# ---------------------------------------------------------------------------
def fetch_disclosures(start, end=None):
    """start〜end（date）の適時開示を返す。取得失敗時はNone（0件の空リストとは区別する）。"""
    end = end or start
    span = f'{start:%Y%m%d}' if start == end else f'{start:%Y%m%d}-{end:%Y%m%d}'
    url = f'{TDNET_API_BASE}/{span}.json'
    try:
        resp = requests.get(url, params={'limit': FETCH_LIMIT}, timeout=REQUEST_TIMEOUT_SEC,
                            headers={'User-Agent': 'Mozilla/5.0 (KurosukeBot disclosure_picks.py)'})
        resp.raise_for_status()
        items = resp.json().get('items', [])
    except Exception as e:
        print(f'[disclosure_picks] 適時開示の取得に失敗: {e}')
        return None
    return [i['Tdnet'] for i in items if isinstance(i, dict) and 'Tdnet' in i]


def code4(company_code):
    """TDnetの5桁コード（末尾に0が付く）を、通常の4桁コードに直す。例：72760→7276、219A0→219A"""
    return (company_code or '')[:4]


# ---------------------------------------------------------------------------
# 分類（タイトルのキーワード）
# ---------------------------------------------------------------------------
def is_dividend_hike(t):
    return '増配' in t and not any(k in t for k in ('減配', '無配'))


def is_dividend_cut(t):
    return any(k in t for k in ('減配', '無配')) and '増配' not in t


def is_benefit_abolish(t):
    return '優待' in t and any(k in t for k in ('廃止', '終了'))


def is_benefit_up(t):
    return ('優待' in t and any(k in t for k in ('拡充', '充実', '増額', '新設', '導入', '拡大'))
            and not is_benefit_abolish(t))


def is_benefit_change(t):
    return ('優待' in t and any(k in t for k in ('変更', '見直し'))
            and not is_benefit_up(t) and not is_benefit_abolish(t))


# 表示順。(キー, 見出し, 絵文字, 判定関数)
CATEGORIES = [
    ('hike', '増配', '📈', is_dividend_hike),
    ('cut', '減配・無配', '📉', is_dividend_cut),
    ('benefit_up', '優待の拡充・新設', '🎁', is_benefit_up),
    ('benefit_down', '優待の廃止・終了', '⚠️', is_benefit_abolish),
    ('benefit_change', '優待の変更（拡充か改悪かは不明）', '🔁', is_benefit_change),
]


def extract_picks(disclosures):
    """種類ごとに、1社1件・新しい順で返す。{key: [開示, ...]}"""
    picks = {key: [] for key, _, _, _ in CATEGORIES}
    seen = {key: set() for key, _, _, _ in CATEGORIES}
    for d in sorted(disclosures, key=lambda x: x.get('pubdate', ''), reverse=True):
        title = d.get('title', '')
        code = code4(d.get('company_code'))
        if not title or not code:
            continue
        for key, _, _, pred in CATEGORIES:
            if pred(title) and code not in seen[key]:
                seen[key].add(code)
                picks[key].append(d)
    return picks


def filter_by_date(picks, day_iso):
    """pubdateが指定日（YYYY-MM-DD）のものだけに絞る。"""
    return {key: [d for d in items if d.get('pubdate', '')[:10] == day_iso] for key, items in picks.items()}


def total_count(picks):
    return sum(len(v) for v in picks.values())


# ---------------------------------------------------------------------------
# 「割安かも」判定との重なり
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# 表示
# ---------------------------------------------------------------------------
def _tags(key, title):
    tags = []
    if key == 'hike':
        if '中間配当' in title:
            tags.append('中間')
        if '期末配当' in title:
            tags.append('期末')
        if '上方修正' in title:
            tags.append('業績上方修正も')
    elif key == 'cut':
        tags.append('無配' if '無配' in title else '減配')
    elif key == 'benefit_up':
        for word in ('新設', '導入', '拡充', '充実', '増額', '拡大'):
            if word in title:
                tags.append(word)
                break
        if '変更' in title:
            tags.append('条件変更も')
    elif key == 'benefit_down':
        tags.append('廃止' if '廃止' in title else '終了')
    return tags


def format_line(d, long_map, key):
    code = code4(d.get('company_code'))
    name = d.get('company_name') or code
    label = f'{name}（{code}）'
    url = (d.get('document_url') or '').replace(MIRROR_REDIRECT_PREFIX, '')  # 中継URLを外し、TDnetのPDFへ直接リンク
    head = f'[{label}]({url})' if url else label
    pub = d.get('pubdate', '')[5:10].replace('-', '/')
    line = f'・{head} {pub}'
    tags = _tags(key, d.get('title', ''))
    if tags:
        line += ' ' + '・'.join(tags)
    if code in long_map:
        score = long_map[code]
        line += f' 🎯割安かも{score:.0f}点' if score is not None else ' 🎯割安かも'
    return line


def build_sections(picks, long_map, caps):
    """種類ごとの見出し＋行（「割安かも」と重なる銘柄を先に、最大caps[key]社）を1つの文字列にする。"""
    blocks = []
    for key, label, emoji, _ in CATEGORIES:
        items = picks.get(key) or []
        if not items:
            continue
        ordered = sorted(items, key=lambda d: 0 if code4(d.get('company_code')) in long_map else 1)
        cap = caps.get(key, 3)
        lines = [format_line(d, long_map, key) for d in ordered[:cap]]
        if len(ordered) > cap:
            lines.append(f'…ほか{len(ordered) - cap}社')
        blocks.append(f'**{emoji} {label}（{len(items)}社）**\n' + '\n'.join(lines))
    return '\n'.join(blocks)


EXPLAIN = '💡 増配・減配＝株主への配当が増える・減る知らせ。優待の廃止・変更は「もらえる内容」が変わる知らせ。株価の反応は「予想とのズレ」次第です。'
NOTE = '※開示タイトルから機械的に抽出（本文は未確認）。🎯＝このシステムの「割安かも」でもある銘柄。売買のおすすめではありません。'
