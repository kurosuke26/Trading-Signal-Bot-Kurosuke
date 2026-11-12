#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
swing_live.py — スイング3型（swing_strategies.py）の毎日の判定と仮想売買の記録（2026-11-11追加）。

本番LONG/SHORT・イベント型（event_strategies.py）とは別枠。実際の売買は行わない（記録だけ）。
collector.py が日足を取り終えたあとに run(histories, names) を1回呼ぶ。投稿は swing_poster.py が行う
（Discord の別チャンネル。DISCORD_WEBHOOK_URL_SWING_PROD／_TEST）。

- 判定：その日の引けまでの日足（Yahoo Finance・collector.py が取得したもの）だけで、検証と同じ関数で判定
- 型③の上方修正：TDnet の業績予想の修正の開示。【2026-11-12変更】XBRL・PDFの数字（disclosure_numbers.py）で
  会社予想が +5%以上 上がったものだけにする（検証と同じ）。数字が読めない開示は、タイトルに「上方修正」とあるものだけ
  「修正率は確認できず」として残す
- 決算発表の予定（earnings_calendar.py・J-Quants）：候補・保有中の銘柄が次の営業日に決算発表なら「決算またぎ注意」
- 取引できない銘柄の除外：event_strategies.tradeable_flags（検証の universe_filters に準拠）
- 地合い：event_strategies.market_index（前日に取引可能だった銘柄の等金額指数）が75日線より上か
- 仮想売買：シグナル翌営業日の始値で約定。手仕舞いは swing_strategies.walk_position（検証と同じ）。
  保有中・約定待ちのポジションは毎日、最新の日足で最初から計算し直す（何度呼んでも同じ結果。分割にも追随）
記録：data/swing_trade_log.json ／ 投稿用の結果：data/swing_scan.json
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests

import disclosure_numbers as dn
import earnings_calendar as ec
import event_strategies as ev
import swing_strategies as sw
from util import drop_unclosed_bars, json_default

JST = timezone(timedelta(hours=9))
SWING_LOG_PATH = os.getenv('SWING_LOG_PATH') or 'data/swing_trade_log.json'
SWING_SCAN_PATH = os.getenv('SWING_SCAN_PATH') or 'data/swing_scan.json'
TDNET_LOOKBACK_DAYS = 5
COST_PCT = 0.3  # 成績の記録に使う往復コスト（検証の厳しめの値）
MIN_ROWS = 140

# 型ごとの運用ルール（保有期間の上限・ストップ・地合いフィルター）。
# 【2026-11-12】地合いフィルターは外す（False）。evaluate_swing.py の検証で、3型とも同じ手仕舞いのまま地合いフィルターを
# 外したほうが1回あたりの「市場との差」が同じか良かった（例：10日・25日線割れの検証期間で ①+0.21%→+0.31%、
# ②−0.27%→−0.11%、③+0.87%→+1.00%）。地合いが悪い日は投稿に注意書きを出すだけにする。
LIVE_RULES = {
    sw.PULLBACK: (10, 'ma25', False),
    sw.BREAKOUT: (10, 'ma25', False),
    sw.REVISION: (10, 'ma25', False),
}
CLOSE_CONFIRMED_JST = (15, 45)  # 東証の大引け（15:30）＋余裕。これより前に取った「今日の足」は場中の値なので使わない


# ---------------------------------------------------------------------------
# TDnet：上方修正
# ---------------------------------------------------------------------------

def is_upward_revision(title):
    t = title or ''
    return '上方修正' in t and '下方修正' not in t


def is_revision_candidate(title):
    """業績予想の修正の開示（タイトルで下方修正と分かるものは除く）。上方か下方かは数字で確かめる。"""
    t = title or ''
    if '下方修正' in t:
        return False
    return '上方修正' in t or (('業績予想' in t or '業績見通し' in t or '通期予想' in t) and '修正' in t)


def fetch_tdnet_upward_revisions(today_jst, lookback_days=TDNET_LOOKBACK_DAYS, session=None):
    """直近 lookback_days 日の開示から上方修正を返す。[{ticker, name, title, disc_date, disc_time, url}]。取得失敗の日は飛ばす。"""
    session = session or requests.Session()
    out, failed = [], 0
    for back in range(lookback_days):
        d = today_jst - timedelta(days=back)
        try:
            resp = session.get(ev.TDNET_LIST_URL.format(date=d.strftime('%Y%m%d')), params={'limit': 10000}, timeout=30)
            resp.raise_for_status()
            items = resp.json().get('items', [])
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f'[swing] TDnet {d:%Y-%m-%d} の取得に失敗（この日はスキップ）: {e}', file=sys.stderr)
            continue
        for it in items:
            t = it.get('Tdnet') or {}
            if not is_revision_candidate(t.get('title')):
                continue
            ticker = ev.tdnet_code_to_ticker(t.get('company_code'))
            if not ticker:
                continue
            pub = str(t.get('pubdate') or '')
            out.append({'ticker': ticker, 'name': t.get('company_name'), 'title': t.get('title'),
                        'disc_date': pub[:10] or d.strftime('%Y-%m-%d'), 'disc_time': pub[11:16],
                        'url': t.get('document_url'), 'url_xbrl': t.get('url_xbrl'),
                        'title_up': is_upward_revision(t.get('title'))})
    return out, failed


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------

def _f(x, d=4):
    return None if x is None or not np.isfinite(x) else round(float(x), d)


def scan(histories, names, revisions, last_date):
    """その日（last_date）にシグナルが出た銘柄を型ごとに返す（並べ順つき・上位に絞る前）。"""
    rev_by_ticker = {}
    for r in revisions:
        rev_by_ticker.setdefault(r['ticker'], []).append(r)
    found = {k: [] for k in sw.TYPES}
    for t, df in sorted(histories.items()):
        if df is None or len(df) < MIN_ROWS or df.index[-1].strftime('%Y-%m-%d') != last_date:
            continue
        f = sw.compute_features(df)
        if not bool(ev.tradeable_flags(df, f['atr']).iloc[-1]):
            continue
        dates = [d.strftime('%Y-%m-%d') for d in df.index]
        revs = rev_by_ticker.get(t, [])
        react = sw.reaction_dates(dates, [(r['disc_date'], r['disc_time']) for r in revs])
        reaction = pd.Series([d in react for d in dates], index=df.index)
        hits = {sw.PULLBACK: sw.pullback_mask(f).iloc[-1], sw.BREAKOUT: sw.breakout_mask(f).iloc[-1],
                sw.REVISION: sw.revision_mask(f, reaction).iloc[-1]}
        row = f.iloc[-1]
        for kind, hit in hits.items():
            if not bool(hit):
                continue
            rk = sw.rank_key(kind, f).iloc[-1]
            item = {'ticker': t, 'name': names.get(t) or t, 'close': _f(row['close'], 1), 'ma25': _f(row['ma25'], 1),
                    'ma75': _f(row['ma75'], 1), 'rsi': _f(row['rsi'], 1), 'ret1_pct': _f(row['ret1'] * 100, 2),
                    'ret5_pct': _f(row['ret5'] * 100, 2), 'ret120_pct': _f(row['ret120'] * 100, 1),
                    'vol_ratio': _f(row['vol_ratio'], 2), 'atr': _f(row['atr'], 2),
                    'rank': float(rk) if np.isfinite(rk) else -1e9}
            if kind == sw.REVISION:
                # その日が反応日になる開示だけ（古い開示は関係ない）
                item['disclosures'] = [{k: r.get(k) for k in ('title', 'url', 'url_xbrl', 'disc_date', 'title_up')}
                                       for r in revs
                                       if sw.reaction_dates(dates, [(r['disc_date'], r['disc_time'])]) == {last_date}]
            found[kind].append(item)
    for kind in found:
        found[kind].sort(key=lambda x: (-x['rank'], x['ticker']))
    return found


# ---------------------------------------------------------------------------
# 仮想売買
# ---------------------------------------------------------------------------

def verify_revisions(items, min_pct=sw.REVISION_MIN_PCT, reader=None):
    """
    型③の候補の開示の数字を読み、+min_pct%以上の上方修正だけ残す。数字が読めないものはタイトルに「上方修正」が
    あるときだけ残す（item['revision'] に結果を入れる）。数字を読むのはシグナルが出た銘柄だけ（通信を減らすため）。
    """
    reader = reader or dn.revision_numbers
    kept = []
    for item in items:
        best, title_up = None, False
        for d in item.get('disclosures') or []:
            title_up = title_up or bool(d.get('title_up'))
            r = reader(d)
            if r and (best is None or r['change_pct'] > best['change_pct']):
                best = dict(r, title=d.get('title'), url=d.get('url'))
        if best is not None:
            if best['change_pct'] >= min_pct:
                item['revision'] = best
                kept.append(item)
        elif title_up:
            item['revision'] = {'item': None, 'change_pct': None, 'source': None}
            kept.append(item)
    return kept


def _load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f'[swing] {path} の読み込みに失敗、初期状態で再開します: {e}', file=sys.stderr)
        return default


def _save_json(path, data):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1, default=json_default)
    os.replace(tmp, path)


def _level(index_df, date):
    sub = index_df.loc[:pd.Timestamp(date)] if index_df is not None and not index_df.empty else None
    return float(sub['level'].iloc[-1]) if sub is not None and len(sub) else None


def update_position(p, df, index_df):
    """約定待ち・保有中のポジションを最新の日足で計算し直す（冪等）。"""
    dates = [d.strftime('%Y-%m-%d') for d in df.index]
    e = next((k for k, d in enumerate(dates) if d > p['signal_date']), None)
    if e is None or not df['Open'].iloc[e] > 0:
        p['state'] = 'pending_entry'
        return
    f = sw.compute_features(df)
    O, C = df['Open'].to_numpy(float), df['Close'].to_numpy(float)
    r = sw.walk_position(O, C, f['ma25'].to_numpy(float), f['atr'].to_numpy(float), e, p['hold'], p['stop'])
    p['entry_date'], p['entry_price'] = dates[e], round(float(O[e]), 2)
    p['last_date'], p['last_price'] = dates[-1], round(float(C[-1]), 2)
    if r['state'] == 'closed':
        x = r['x']
        px = float(C[x] if r['at_close'] else O[x])
        p.update(state='closed', exit_date=dates[x], exit_price=round(px, 2),
                 exit_reason=f"保有{p['hold']}営業日目の引け" if r['at_close'] else
                 f"終値が{sw.STOP_LABEL[p['stop']]}の基準を下回った→翌営業日の始値", held_days=x - e + 1)
        ret = (px / O[e] - 1) * 100 - COST_PCT
        a = _level(index_df, p['signal_date'])
        b = _level(index_df, dates[x])
        p['return_pct'] = round(ret, 2)
        p['market_return_pct'] = round((b / a - 1) * 100, 2) if a and b else None
        p['excess_pct'] = round(ret - p['market_return_pct'], 2) if p['market_return_pct'] is not None else None
    elif r['state'] == 'exit_next_open':
        p.update(state='exit_next_open', held_days=len(C) - e,
                 unrealized_pct=round((C[-1] / O[e] - 1) * 100, 2))
    else:
        p.update(state='open', held_days=r['days_held'], stop_level=_f(r['stop_level'], 1),
                 unrealized_pct=round((C[-1] / O[e] - 1) * 100, 2),
                 last_day_tomorrow=r['days_held'] == p['hold'] - 1)


def performance(log):
    out = {}
    for kind in sw.TYPES:
        ps = [p for p in log if p['kind'] == kind]
        closed = [p for p in ps if p['state'] == 'closed']
        rets = [p['return_pct'] for p in closed]
        wins = [x for x in rets if x > 0]
        losses = [x for x in rets if x <= 0]
        ex = [p['excess_pct'] for p in closed if p.get('excess_pct') is not None]
        out[kind] = {'label': sw.TYPE_LABEL[kind], 'closed': len(closed),
                     'open': sum(1 for p in ps if p['state'] in ('open', 'exit_next_open')),
                     'pending_entry': sum(1 for p in ps if p['state'] == 'pending_entry'),
                     'win_rate_pct': round(len(wins) / len(rets) * 100, 1) if rets else None,
                     'avg_return_pct': round(sum(rets) / len(rets), 2) if rets else None,
                     'payoff_ratio': round((sum(wins) / len(wins)) / abs(sum(losses) / len(losses)), 2)
                     if wins and losses and sum(losses) else None,
                     'avg_excess_pct': round(sum(ex) / len(ex), 2) if ex else None}
    return out


# ---------------------------------------------------------------------------
# 1日1回の処理
# ---------------------------------------------------------------------------

def run(histories, names=None, today_utc=None, tdnet_items=None, fetch_missing=True):
    """戻り値：data/swing_scan.json と同じ内容の dict。"""
    names = names or {}
    today_utc = today_utc or datetime.now(timezone.utc)
    today_jst = today_utc.astimezone(JST)
    histories, dropped = drop_unclosed_bars(histories, today_jst)
    if dropped:
        print(f'[swing] 場中に取得した今日（{today_jst:%Y-%m-%d}）の足を{dropped}銘柄で除外しました（引け前の値のため）')
    index_df = ev.market_index(histories)
    if index_df.empty:
        raise RuntimeError('市場平均を計算できる日足がありません')
    last_date = index_df.index[-1].strftime('%Y-%m-%d')
    level = index_df['level']
    ma75 = level.rolling(75).mean()
    regime_ok = bool(level.iloc[-1] > ma75.iloc[-1]) if ma75.iloc[-1] == ma75.iloc[-1] else False
    regime = {'date': last_date, 'ok': regime_ok,
              'index_vs_ma75_pct': _f((level.iloc[-1] / ma75.iloc[-1] - 1) * 100, 2) if ma75.iloc[-1] == ma75.iloc[-1] else None}

    tdnet_failed = 0
    if tdnet_items is None:
        tdnet_items, tdnet_failed = fetch_tdnet_upward_revisions(today_jst)
    found = scan(histories, names, tdnet_items, last_date)
    n_rev_hits = len(found[sw.REVISION])
    found[sw.REVISION] = verify_revisions(found[sw.REVISION])
    if n_rev_hits:
        print(f'[swing] 型③：株価の条件を満たした{n_rev_hits}件のうち、修正の数字で確かめて{len(found[sw.REVISION])}件を残しました')

    # 決算発表の予定（次の営業日）
    try:
        cal, cal_fetched = ec.update()
    except Exception as e:  # noqa: BLE001
        print(f'[swing] 決算発表予定の処理でエラー（表示なしで続行）: {e}', file=sys.stderr)
        cal, cal_fetched = {}, False
    earn_date, earn_map = ec.next_announcements(cal, last_date)
    for kind in found:
        for item in found[kind]:
            if item['ticker'] in earn_map:
                item['earnings_date'] = earn_date

    log = _load_json(SWING_LOG_PATH, [])
    holding = {(p['kind'], p['ticker']) for p in log if p['state'] != 'closed'}
    signals = {}
    for kind in sw.TYPES:
        hold, stop, use_regime = LIVE_RULES[kind]
        top = found[kind][:sw.TOP_N]
        skipped = bool(use_regime and not regime_ok)
        for item in top:
            item['already_holding'] = (kind, item['ticker']) in holding
            if skipped or item['already_holding']:
                continue
            if any(p['kind'] == kind and p['ticker'] == item['ticker'] and p['signal_date'] == last_date for p in log):
                continue  # 同じ日に2回実行された場合
            log.append({'kind': kind, 'ticker': item['ticker'], 'name': item['name'], 'signal_date': last_date,
                        'hold': hold, 'stop': stop, 'state': 'pending_entry', 'signal': {k: item[k] for k in item
                                                                                        if k not in ('rank', 'disclosures')}})
            holding.add((kind, item['ticker']))
        signals[kind] = {'label': sw.TYPE_LABEL[kind], 'rule': {'hold': hold, 'stop': stop, 'regime': use_regime},
                         'skipped_by_regime': skipped, 'total_hits': len(found[kind]), 'top': top}

    need = {p['ticker'] for p in log if p['state'] != 'closed'}
    missing = sorted(t for t in need if t not in histories)
    extra = ev.fetch_missing_histories(missing) if (fetch_missing and missing) else {}
    extra, _ = drop_unclosed_bars(extra, today_jst)
    closed_today = []
    for p in log:
        if p['state'] == 'closed':
            continue
        df = histories.get(p['ticker'])
        if df is None:
            df = extra.get(p['ticker'])
        if df is None or df.empty:
            continue
        update_position(p, df, index_df)
        p['earnings_date'] = earn_date if (p['state'] != 'closed' and p['ticker'] in earn_map) else None
        if p['state'] == 'closed' and p.get('exit_date') == last_date:
            closed_today.append(p)
    _save_json(SWING_LOG_PATH, log)

    scan_out = {
        'generated_at_utc': today_utc.isoformat(), 'date': last_date, 'regime': regime,
        'tdnet': {'revision_disclosures': len(tdnet_items), 'failed_days': tdnet_failed},
        'earnings': {'date': earn_date, 'count': len(earn_map), 'fetched': cal_fetched,
                     'holding': [p for p in log if p['state'] != 'closed' and p.get('earnings_date')]},
        'signals': signals,
        'exits_next_open': [p for p in log if p['state'] == 'exit_next_open'],
        'exits_tomorrow_close': [p for p in log if p['state'] == 'open' and p.get('last_day_tomorrow')],
        'closed_today': closed_today,
        'open_positions': [p for p in log if p['state'] == 'open'],
        'pending_entry': [p for p in log if p['state'] == 'pending_entry'],
        'performance': performance(log),
    }
    _save_json(SWING_SCAN_PATH, scan_out)
    return scan_out
