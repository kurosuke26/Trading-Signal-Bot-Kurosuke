#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
earnings_calendar.py — 決算発表の予定日（J-Quants）を取って貯める（2026-11-12追加）。

J-Quants Free プランの /v2/equities/earnings-calendar は「翌営業日に決算発表を予定している会社」だけを返す
（2026-11-12 に確認。日付を指定しても翌営業日の分だけ）。そこで毎晩取得して data/earnings_calendar.json に
貯めていく（あとで「決算の直前に買わない」などのルールを検証するのにも使える）。

swing_live.py が使う：候補・保有中の銘柄が、次の営業日に決算発表を予定していれば「決算またぎ注意」と表示する。
APIキー（環境変数 JQUANTS_API_KEY）が無い・取得に失敗した場合は、貯めてある分だけで動く（無ければ表示しない）。
"""

import json
import os
import sys
from datetime import datetime, timedelta

CAL_PATH = os.getenv('EARNINGS_CALENDAR_PATH') or 'data/earnings_calendar.json'
KEEP_DAYS = 400


def code_to_ticker(code):
    code = str(code or '').strip()
    if len(code) == 5 and code.endswith('0'):
        code = code[:4]
    return f'{code}.T' if len(code) == 4 else None


def load(path=None):
    path = path or CAL_PATH
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f'[earnings] {path} の読み込みに失敗（空で再開）: {e}', file=sys.stderr)
        return {}


def save(cal, path=None):
    path = path or CAL_PATH
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    cutoff = (datetime.now() - timedelta(days=KEEP_DAYS)).strftime('%Y-%m-%d')
    cal = {d: v for d, v in sorted(cal.items()) if d >= cutoff}
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(cal, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def fetch():
    """J-Quants から翌営業日の予定を取る。戻り値 {日付: {銘柄: {'name','fq','fy'}}}。失敗時は None。"""
    try:
        import jquants_client as jq
        rows = jq._get('/equities/earnings-calendar', None, max_retries=3)
    except Exception as e:  # noqa: BLE001
        print(f'[earnings] 決算発表予定の取得に失敗（貯めてある分だけで続行）: {e}', file=sys.stderr)
        return None
    out = {}
    for r in rows or []:
        t = code_to_ticker(r.get('Code'))
        if not t or not r.get('Date'):
            continue
        out.setdefault(r['Date'], {})[t] = {'name': r.get('CoName'), 'fq': r.get('FQ'), 'fy': r.get('FY')}
    return out


def update(path=None):
    """取得して貯める。戻り値：(貯めてある全予定, 今回取得できたか)。"""
    cal = load(path)
    new = fetch()
    if new:
        for d, v in new.items():
            cal.setdefault(d, {}).update(v)
        save(cal, path)
    return cal, bool(new)


def next_announcements(cal, after_date):
    """after_date より後で最も早い予定日と、その日の銘柄 {銘柄: 情報}。無ければ (None, {})。"""
    days = sorted(d for d in cal if d > after_date)
    return (days[0], cal[days[0]]) if days else (None, {})
