#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
market_calendar.py — 投資初心者向け「今日の予定」の材料を作る（breaking_alerts.pyから呼ぶ）

計算で出せるもの（外部データ不要）:
  - 東証の休場日（土日・祝日・年末年始 12/31〜1/3）
  - SQ日（毎月第2金曜。休場日なら前の営業日）
  - 権利付き最終日（月末の最終営業日＝権利確定日の2営業日前。2019年からの受渡しT+2）と、
    その翌営業日の権利落ち日
公式日程を手入力するもの:
  - 米雇用統計・米CPI・FOMC・日銀会合（economic_calendar.json）

【正直な注意点】
- 権利確定日は「月末」の会社が大半だが、20日締め等の会社もある。ここでは月末分だけを扱う。
- 祝日判定は jpholiday（祝日法ベース）。臨時休場などは反映されない。
"""

import json
import os
import sys
from datetime import date, timedelta

try:
    import jpholiday
except ImportError:  # 未インストールでも土日と年末年始だけで動かす
    jpholiday = None

CALENDAR_PATH = os.getenv('ECONOMIC_CALENDAR_PATH') or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'economic_calendar.json')
# この時刻（JST）より前の予定は、前日朝の投稿で「明朝」として先に知らせる
EARLY_MORNING_CUTOFF = '07:00'
WEEKDAY_JA = '月火水木金土日'


# ---------------------------------------------------------------------------
# 営業日
# ---------------------------------------------------------------------------
def closed_reason(d):
    """東証が休みならその理由、開いていればNone。"""
    if d.weekday() >= 5:
        return '土日'
    if (d.month, d.day) in ((12, 31), (1, 1), (1, 2), (1, 3)):
        return '年末年始'
    if jpholiday is not None:
        name = jpholiday.is_holiday_name(d)
        if name:
            return name
    return None


def is_market_open(d):
    return closed_reason(d) is None


def add_business_days(d, n):
    """n営業日後（nが負なら前）の日付。"""
    step = 1 if n >= 0 else -1
    remaining = abs(n)
    while remaining:
        d += timedelta(days=step)
        if is_market_open(d):
            remaining -= 1
    return d


def last_business_day_of_month(year, month):
    d = (date(year + (month == 12), month % 12 + 1, 1)) - timedelta(days=1)
    while not is_market_open(d):
        d -= timedelta(days=1)
    return d


def rights_last_day(year, month):
    """月末が権利確定日の銘柄の権利付き最終日。"""
    return add_business_days(last_business_day_of_month(year, month), -2)


def sq_day(year, month):
    d = date(year, month, 1)
    d += timedelta(days=(4 - d.weekday()) % 7 + 7)  # 第2金曜
    while not is_market_open(d):
        d -= timedelta(days=1)
    return d


# ---------------------------------------------------------------------------
# 経済イベント（economic_calendar.json）
# ---------------------------------------------------------------------------
def load_economic_calendar():
    try:
        with open(CALENDAR_PATH, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f'[market_calendar] {CALENDAR_PATH} の読み込みに失敗: {e}', file=sys.stderr)
        return {'event_types': {}, 'events': []}


def _is_early(time_jst):
    # 'HH:MM'形式だけを時刻として比較する（'昼ごろ'等は早朝扱いしない）
    return len(time_jst) == 5 and time_jst[2] == ':' and time_jst < EARLY_MORNING_CUTOFF


def economic_events_for_morning(today, calendar):
    """今朝の投稿に載せる予定。今日の7時以降の予定＋明日の早朝(7時前)の予定。"""
    tomorrow = today + timedelta(days=1)
    picked = []
    for ev in calendar.get('events', []):
        try:
            ev_date = date.fromisoformat(ev['date_jst'])
        except (KeyError, ValueError):
            continue
        time_jst = ev.get('time_jst', '')
        if ev_date == today and not _is_early(time_jst):
            picked.append(('今日', ev))
        elif ev_date == tomorrow and _is_early(time_jst):
            picked.append(('明朝', ev))
    picked.sort(key=lambda p: (p[0] != '今日', p[1].get('time_jst', '')))
    return picked


def days_of_calendar_left(today, calendar):
    """登録済みの最後の予定まで何日あるか（予定が無ければ-1）。"""
    dates = []
    for ev in calendar.get('events', []):
        try:
            dates.append(date.fromisoformat(ev['date_jst']))
        except (KeyError, ValueError):
            pass
    return (max(dates) - today).days if dates else -1


# ---------------------------------------------------------------------------
# 「今日の予定」の本文
# ---------------------------------------------------------------------------
def _rights_month_note(month):
    if month == 3:
        return '3月は1年でいちばん対象の会社が多い月です。'
    if month == 9:
        return '9月は3月に次いで対象の会社が多い月です（中間配当・優待）。'
    return ''


SQ_EXPLAIN = ('先物・オプション取引の清算日。朝の寄り付きに大きな売買が出て、'
              '値動きが荒くなることがあります')
EX_RIGHTS_EXPLAIN = ('前日で配当・優待の権利が確定したため、配当の分ほど株価が下がって始まりやすい日です。'
                     '値下がりに見えても、多くは権利の分が差し引かれただけです')
HOLIDAY_EXPLAIN = '日本株の売買はできません。注文は次の営業日に持ち越されます'


def market_day_flags(d):
    """その日の株式市場の出来事を (種類, 付加情報) のリストで返す（今日の予定・来週の予定で共通）。
    種類: 'holiday'(祝日名) / 'sq'(メジャーならTrue) / 'rights_last'(対象の月) / 'ex_rights'(None)"""
    reason = closed_reason(d)
    if reason:
        return [] if reason == '土日' else [('holiday', reason)]

    flags = []
    if d == sq_day(d.year, d.month):
        flags.append(('sq', d.month in (3, 6, 9, 12)))
    if d == rights_last_day(d.year, d.month):
        flags.append(('rights_last', d.month))
    prev_month = (d.year, d.month - 1) if d.month > 1 else (d.year - 1, 12)
    for ym in (prev_month, (d.year, d.month)):
        if add_business_days(rights_last_day(*ym), 1) == d:
            flags.append(('ex_rights', None))
    return flags


def build_morning_items(today, calendar=None):
    """(見出し, 解説) のリスト。何も無い日は空リスト。"""
    if calendar is None:
        calendar = load_economic_calendar()
    items = []

    for kind, extra in market_day_flags(today):
        if kind == 'holiday':
            items.append((f'🏖 今日は東証はお休みです（{extra}）', HOLIDAY_EXPLAIN))
        elif kind == 'sq':
            label = 'メジャーSQ' if extra else 'SQ'
            items.append((f'⚖️ 今日は{label}日です',
                          SQ_EXPLAIN + ('（3・6・9・12月は特に大きい）' if extra else '')))
        elif kind == 'rights_last':
            items.append((f'🎁 今日は{extra}月末の配当・株主優待の「権利付き最終日」です',
                          '今日の取引終了（15:30）までに買って持っていれば、配当・優待をもらう権利が得られます。'
                          + _rights_month_note(extra)))
        elif kind == 'ex_rights':
            items.append(('📉 今日は「権利落ち日」です', EX_RIGHTS_EXPLAIN))

    if is_market_open(today):
        rl = rights_last_day(today.year, today.month)
        if today != rl and add_business_days(today, 2) == rl:
            items.append((f'🎁 {rl.month}/{rl.day}（{WEEKDAY_JA[rl.weekday()]}）が{today.month}月末の配当・優待の「権利付き最終日」です（あと2営業日）',
                          'その日の取引終了までに買って持っている必要があります。'
                          + _rights_month_note(today.month)))

    for when, ev in economic_events_for_morning(today, calendar):
        info = calendar.get('event_types', {}).get(ev.get('type'), {})
        title = info.get('title', ev.get('type', '予定'))
        if ev.get('note'):
            title += f'（{ev["note"]}）'
        time_jst = ev.get('time_jst', '')
        items.append((f'🗓 {when} {time_jst} {title}', info.get('explain', '')))

    return items


def format_morning_message(today, items):
    header = f'📅 **今日の予定（{today.month}/{today.day} {WEEKDAY_JA[today.weekday()]}）**'
    lines = [header]
    for headline, explain in items:
        lines.append(f'・**{headline}**')
        if explain:
            lines.append(f'　💡 {explain}')
    return '\n'.join(lines)


def build_week_text(start, days=7, calendar=None):
    """「来週の予定」（weekly_tips.py用）。start から days 日分を日付ごとに1行で並べ、
    出てきた用語の解説を最後にまとめる。何も無ければその旨の1文。"""
    if calendar is None:
        calendar = load_economic_calendar()
    event_types = calendar.get('event_types', {})
    lines = []
    glossary = {}

    for offset in range(days):
        d = start + timedelta(days=offset)
        entries = []
        for kind, extra in market_day_flags(d):
            if kind == 'holiday':
                entries.append(f'🏖 東証休場（{extra}）')
            elif kind == 'sq':
                entries.append('⚖️ メジャーSQ日' if extra else '⚖️ SQ日')
                glossary['SQ日'] = SQ_EXPLAIN
            elif kind == 'rights_last':
                entries.append(f'🎁 {extra}月末の権利付き最終日')
                glossary['権利付き最終日'] = 'この日の取引終了までに買って持っていれば、配当・株主優待をもらう権利が得られる'
            elif kind == 'ex_rights':
                entries.append('📉 権利落ち日')
                glossary['権利落ち日'] = EX_RIGHTS_EXPLAIN
        day_events = [ev for ev in calendar.get('events', []) if ev.get('date_jst') == d.isoformat()]
        for ev in sorted(day_events, key=lambda e: e.get('time_jst', '')):
            info = event_types.get(ev.get('type'), {})
            title = info.get('title', ev.get('type', '予定'))
            if ev.get('note'):
                title += f'（{ev["note"]}）'
            entries.append(f'🗓 {ev.get("time_jst", "")} {title}')
            if info.get('explain'):
                glossary[info.get('title', ev.get('type'))] = info['explain']
        if entries:
            lines.append(f'**{d.month}/{d.day}（{WEEKDAY_JA[d.weekday()]}）** ' + ' ／ '.join(entries))

    if not lines:
        return '来週は、休場日や大きな経済指標の発表などの予定はありません。'
    lines.append('')
    lines += [f'💡 {term}：{explain}' for term, explain in glossary.items()]
    lines.append('※時刻は日本時間。米国の発表は日本時間の夜〜早朝です')
    return '\n'.join(lines)


if __name__ == '__main__':
    # 動作確認用：python market_calendar.py 2026-09-28 のように日付を渡すと、その日の朝の投稿文を表示
    target = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date.today()
    found = build_morning_items(target)
    print(format_morning_message(target, found) if found else f'{target}: 予定なし')
