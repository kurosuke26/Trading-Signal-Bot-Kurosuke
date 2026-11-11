#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
swing_poster.py — スイング3型（swing_live.py の data/swing_scan.json）を Discord の専用チャンネルに投稿する（2026-11-11追加）。

本番の poster.py とは別。Webhook は環境変数 DISCORD_WEBHOOK_URL_SWING_PROD（本番）／DISCORD_WEBHOOK_URL_SWING_TEST（テスト）。
両方あれば両方に送る。どちらも無ければ何もしない。

  python swing_poster.py            # 投稿
  python swing_poster.py --dry-run  # 送らずに内容を表示
  python swing_poster.py --force    # 投稿済みの判定日でも投稿する
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

import swing_strategies as sw
from util import json_default

SCAN_PATH = os.getenv('SWING_SCAN_PATH') or 'data/swing_scan.json'
MAX_AGE_HOURS = float(os.getenv('SWING_MAX_AGE_HOURS', '20'))
MARKER_PATH = os.getenv('SWING_POSTED_MARKER') or 'data/_last_swing_posted_date.txt'  # 同じ判定日の二重投稿を防ぐ
COLOR = {sw.PULLBACK: 0x3498DB, sw.BREAKOUT: 0xE67E22, sw.REVISION: 0x9B59B6, 'EXIT': 0xE74C3C, 'INFO': 0x95A5A6}
MAX_EMBEDS_PER_POST = 5  # poster.py と同じ理由（Discordの実際の上限は公開値より厳しい）
MAX_CHARS_PER_POST = 4000


def _code(t):
    return t.replace('.T', '')


def _yen(v):
    return '—' if v is None else f'{v:,.0f}円'


def _pct(v, d=1):
    return '—' if v is None else f'{v:+.{d}f}%'


def _rule_text(rule):
    return (f"買い：翌営業日の寄り付き／手仕舞い：{rule['hold']}営業日目の引け、または"
            f"{'終値が25日線を割ったら翌営業日の寄り付き' if rule['stop'] == 'ma25' else sw.STOP_LABEL[rule['stop']] + 'の切り上げストップ（終値で判定→翌営業日の寄り付き）'}"
            f"{'／地合いフィルターあり' if rule['regime'] else ''}")


def _signal_line(kind, x):
    head = f"**{_code(x['ticker'])} {x['name']}**　終値 {_yen(x['close'])}"
    if kind == sw.PULLBACK:
        body = (f"25日線 {_yen(x['ma25'])}／75日線 {_yen(x['ma75'])}｜5日 {_pct(x['ret5_pct'])}｜"
                f"120日 {_pct(x['ret120_pct'], 0)}｜RSI {x['rsi']:.0f}")
    elif kind == sw.BREAKOUT:
        body = (f"60日高値を更新｜出来高 {x['vol_ratio']:.1f}倍｜25日線 {_yen(x['ma25'])}"
                f"（{_pct((x['close'] / x['ma25'] - 1) * 100 if x['ma25'] else None)}）")
    else:
        body = f"反応日 {_pct(x['ret1_pct'])}｜出来高 {x['vol_ratio']:.1f}倍｜25日線 {_yen(x['ma25'])}"
        d = (x.get('disclosures') or [])[:1]
        if d:
            body += f"\n　└ [{d[0]['title'][:40]}]({d[0]['url']})" if d[0].get('url') else f"\n　└ {d[0]['title'][:40]}"
    note = '（保有中のため記録には追加しない）' if x.get('already_holding') else ''
    return f"{head}{note}\n{body}"


def build_embeds(scan):
    embeds = []
    reg = scan['regime']
    reg_text = (f"{'☀ 良好' if reg['ok'] else '☂ 悪化'}（市場平均が75日線から {_pct(reg.get('index_vs_ma75_pct'), 2)}）")
    embeds.append({
        'title': f"📈 スイング候補（{scan['date']}の引けで判定）",
        'description': (f"地合い：{reg_text}\n"
                        '数日〜1か月で利益を狙う3つの型です。本番のロングシグナルとは別枠で、仮想売買の成績を記録しています。\n'
                        '※過去2年の検証では、3型とも市場平均に対する優位性は統計的に確認できていません（検証中）。'
                        '投資判断はご自身で行ってください。'),
        'color': COLOR['INFO']})
    for kind in sw.TYPES:
        s = scan['signals'].get(kind)
        if not s:
            continue
        if s['skipped_by_regime']:
            desc = f"地合い悪化のため新規の買いは見送り（該当 {s['total_hits']}件）"
        elif not s['top']:
            desc = '本日の該当なし'
        else:
            desc = '\n\n'.join(_signal_line(kind, x) for x in s['top'])
            if s['total_hits'] > len(s['top']):
                desc += f"\n\n（該当 {s['total_hits']}件のうち上位{len(s['top'])}件）"
        embeds.append({'title': s['label'], 'description': desc[:3500], 'color': COLOR[kind],
                       'footer': {'text': _rule_text(s['rule'])}})

    lines = []
    for p in scan.get('exits_next_open', []):
        lines.append(f"🔴 **{_code(p['ticker'])} {p['name']}**（{sw.TYPE_LABEL[p['kind']][:1]}）明日の寄り付きで手仕舞い"
                     f"｜含み {_pct(p.get('unrealized_pct'))}｜{sw.STOP_LABEL[p['stop']]}に抵触")
    for p in scan.get('exits_tomorrow_close', []):
        lines.append(f"🟡 **{_code(p['ticker'])} {p['name']}**（{sw.TYPE_LABEL[p['kind']][:1]}）明日の引けで手仕舞い"
                     f"（{p['hold']}営業日目）｜含み {_pct(p.get('unrealized_pct'))}")
    for p in scan.get('closed_today', []):
        lines.append(f"✅ **{_code(p['ticker'])} {p['name']}**（{sw.TYPE_LABEL[p['kind']][:1]}）本日決済 "
                     f"{_pct(p.get('return_pct'))}（市場との差 {_pct(p.get('excess_pct'))}）")
    if lines:
        embeds.append({'title': '手仕舞いの予定・本日の決済', 'description': '\n'.join(lines)[:3500], 'color': COLOR['EXIT']})

    held = scan.get('open_positions', [])
    perf = scan.get('performance', {})
    plines = []
    for kind in sw.TYPES:
        pf = perf.get(kind) or {}
        hs = [p for p in held if p['kind'] == kind]
        head = f"**{sw.TYPE_LABEL[kind]}**　保有{pf.get('open', 0)}件・約定待ち{pf.get('pending_entry', 0)}件"
        if pf.get('closed'):
            head += (f"\n　決済{pf['closed']}件｜勝率 {pf['win_rate_pct']}%｜平均 {_pct(pf['avg_return_pct'], 2)}"
                     f"｜ペイオフ {pf['payoff_ratio'] or '—'}｜市場との差 {_pct(pf['avg_excess_pct'], 2)}")
        if hs:
            head += '\n　' + '、'.join(f"{_code(p['ticker'])}（{p['held_days']}日目 {_pct(p.get('unrealized_pct'))}）"
                                      for p in hs[:12])
        plines.append(head)
    embeds.append({'title': '保有中・仮想売買の成績（往復コスト0.3%込み）', 'description': '\n\n'.join(plines)[:3500],
                   'color': COLOR['INFO']})
    if scan.get('tdnet', {}).get('failed_days'):
        embeds[-1]['footer'] = {'text': f"TDnetの開示一覧を{scan['tdnet']['failed_days']}日分取得できませんでした（③は取りこぼしの可能性あり）"}
    return embeds


def _chars(e):
    return len(e.get('title') or '') + len(e.get('description') or '') + len((e.get('footer') or {}).get('text') or '')


def split_posts(embeds):
    posts, cur, n = [], [], 0
    for e in embeds:
        c = _chars(e)
        if cur and (len(cur) >= MAX_EMBEDS_PER_POST or n + c > MAX_CHARS_PER_POST):
            posts.append(cur)
            cur, n = [], 0
        cur.append(e)
        n += c
    if cur:
        posts.append(cur)
    return posts


def webhook_urls():
    return [(env, os.getenv(f'DISCORD_WEBHOOK_URL_SWING_{env}')) for env in ('TEST', 'PROD')
            if os.getenv(f'DISCORD_WEBHOOK_URL_SWING_{env}')]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--force', action='store_true', help='投稿済みでも投稿する')
    args = ap.parse_args()
    if not os.path.exists(SCAN_PATH):
        print(f'[swing-post] {SCAN_PATH} がありません（収集でスイング判定が動いていない）。投稿しません')
        return 0
    with open(SCAN_PATH, encoding='utf-8') as f:
        scan = json.load(f)
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(scan['generated_at_utc'])).total_seconds() / 3600
    if age > MAX_AGE_HOURS and not args.dry_run:
        print(f'[swing-post] 判定結果が{age:.1f}時間前のもので古いため投稿しません')
        return 0
    if not args.dry_run and not args.force and os.path.exists(MARKER_PATH):
        with open(MARKER_PATH, encoding='utf-8') as f:
            if f.read().strip() == scan['date']:
                print(f"[swing-post] {scan['date']} の判定は投稿済みのため投稿しません")
                return 0
    posts = split_posts(build_embeds(scan))
    if args.dry_run:
        print(json.dumps(posts, ensure_ascii=False, indent=1, default=json_default))
        return 0
    urls = webhook_urls()
    if not urls:
        print('[swing-post] DISCORD_WEBHOOK_URL_SWING_PROD／_TEST が未設定のため投稿しません')
        return 0
    ok = True
    for env, url in urls:
        for k, embeds in enumerate(posts, start=1):
            r = requests.post(url, json={'embeds': embeds}, timeout=30)
            if r.status_code in (200, 204):
                print(f'[swing-post/{env}] 送信しました（{k}/{len(posts)}通目）')
            else:
                ok = False
                print(f'[swing-post/{env}] 送信失敗（{k}/{len(posts)}通目）: {r.status_code} {r.text[:200]}')
            time.sleep(1)
    if ok:
        with open(MARKER_PATH, 'w', encoding='utf-8') as f:
            f.write(scan['date'] + '\n')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
