#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
event_strategies.py — 本番LONG（複合スコア）とは別枠の「イベント型」仮想売買系列（2026-09-16追加）。

先読みなしの検証（search_selection_rules.py、前半で選び後半で確認）で「期待値と市場との差がプラスで安定」
だったルールを、実運用で成績を確かめるために仮想売買として記録する。実際の取引ではない。

【系列1：増配修正の発表翌日に買う（DIVIDEND_HIKE）】
- シグナル：TDnet（やのしんTDnet WEB-API）の適時開示のうち、タイトルに「（増配）」を含む
  配当予想の修正・剰余金の配当のお知らせ（次期・来期の配当予想は除く。減配・無配を含むものは除く）
- 約定：開示日（非営業日の開示はその次の営業日）の翌営業日の始値
- 決済：終値が「終値−ATR(14)×3.0」の切り上げストップに抵触したら、その翌営業日の始値（検証と同じ）
- 同じ銘柄は保有中または直近20営業日以内のシグナルがあれば重複して建てない
- 検証（2025-08-08〜2026-06）：623件・勝率54.4%・ペイオフ2.90・市場平均との差+4.3%/回（前半も+1.8%）

【系列2：暴落後の行動ルール（CRASH_REBOUND）】
- 発動：取引可能な銘柄の等金額指数が、20営業日高値から−10%以下に下落した日（発動後20営業日は再発動しない）
- 対象：その日に取引可能で、20営業日高値から−20%以上下げている銘柄
- 約定：発動日の翌営業日の始値。決済：利確 ATR×4／損切り ATR×2（その日の高値・安値で判定、寄りで越えていたら
  始値、同じ日に両方なら損切り優先）、最長20営業日（その日の終値）
- 検証：前半（2回の暴落）・後半（1回）とも勝率60%以上・ペイオフ1.8以上。ただし暴落の回数が少なく、市場との差は
  後半+0.5%と小さい＝「暴落時の分割買いの目安」として扱う

【データの取り方】
- 株価（始値・高値・安値・終値）は Yahoo Finance（collector.py が取得した日足。無い銘柄はここで個別に取得）
- 適時開示は TDnet（J-Quants Freeプランは12週間遅れのため、当日の開示には使えない）
- 取引可能フィルター（検証の universe_filters.py に準拠）：直近20日の平均売買代金5,000万円以上、
  60日の高値÷安値が3倍未満、ATR÷終値が10%未満、売買代金が前250日平均の10倍未満。
  ※Yahoo Finance には値幅制限（ストップ高・安）のフラグが無いため、その条件だけは使わない

記録：data/event_trade_log.json（本番LONG/SHORTの data/trade_log.json とは別ファイル）
状態：data/event_state.json（処理済みの開示・暴落の発動日など）
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests

JST = timezone(timedelta(hours=9))
EVENT_TRADE_LOG_PATH = os.getenv('EVENT_TRADE_LOG_PATH') or 'data/event_trade_log.json'
EVENT_STATE_PATH = os.getenv('EVENT_STATE_PATH') or 'data/event_state.json'
TDNET_LIST_URL = 'https://webapi.yanoshin.jp/webapi/tdnet/list/{date}.json'
TDNET_LOOKBACK_DAYS = 4  # 収集ジョブが休んだ日の開示も拾う（週末・祝日・失敗の取りこぼし対策）

DIVIDEND_HIKE = 'DIVIDEND_HIKE'
CRASH_REBOUND = 'CRASH_REBOUND'
SERIES_LABEL = {DIVIDEND_HIKE: '増配修正の発表翌日に買う（ATR×3.0トレーリング）',
                CRASH_REBOUND: '暴落後の反発狙い（利確ATR×4・損切りATR×2・最長20営業日）'}
DIVIDEND_TRAIL_ATR = 3.0
DEDUP_TRADING_DAYS = 20
CRASH_INDEX_DD = -0.10
CRASH_STOCK_DD = -0.20
CRASH_TP_ATR, CRASH_SL_ATR, CRASH_MAX_DAYS = 4.0, 2.0, 20
CRASH_COOLDOWN_DAYS = 20
VALUATION_LOT_SHARES = int(os.getenv('VALUATION_LOT_SHARES', '100'))  # 含み損益の円換算：1銘柄あたりの株数
COST_PCT = 0.1
FALLBACK_ATR_PCT = 0.03

MIN_AVG_TRADING_VALUE_YEN = 50_000_000
PRICE_RANGE_RATIO_60 = 3.0
MAX_ATR_PCT = 0.10
SURGE_VALUE_RATIO = 10.0


# ---------------------------------------------------------------------------
# 1. TDnet：増配修正の検出
# ---------------------------------------------------------------------------

_HIKE_DOC = re.compile(r'配当予想|剰余金の配当|剰余金配当|配当金')
_EXCLUDE = ('減配', '無配', '次期', '来期', '翌期')


def is_dividend_hike_revision(title):
    """タイトルが「増配の修正・決定」を明示しているか。方向が書かれていないものは確実性を優先して拾わない。"""
    if not title or '増配' not in title:
        return False
    if any(w in title for w in _EXCLUDE):
        return False
    return bool(_HIKE_DOC.search(title))


def tdnet_code_to_ticker(code):
    code = str(code or '').strip()
    if len(code) == 5 and code.endswith('0'):
        code = code[:4]
    return f'{code}.T' if len(code) == 4 else None


def fetch_tdnet_dividend_hikes(today_jst, session=None, lookback_days=TDNET_LOOKBACK_DAYS):
    """直近 lookback_days 日分の開示一覧から、増配修正を返す。[{ticker, name, title, disc_date, disc_time, url, id}]"""
    session = session or requests.Session()
    out = []
    for back in range(lookback_days):
        d = today_jst - timedelta(days=back)
        url = TDNET_LIST_URL.format(date=d.strftime('%Y%m%d'))
        try:
            resp = session.get(url, params={'limit': 10000}, timeout=30)
            resp.raise_for_status()
            items = resp.json().get('items', [])
        except Exception as e:  # noqa: BLE001
            print(f'[event] TDnet {d:%Y-%m-%d} の取得に失敗（この日はスキップ）: {e}', file=sys.stderr)
            continue
        for it in items:
            t = it.get('Tdnet') or {}
            if not is_dividend_hike_revision(t.get('title')):
                continue
            ticker = tdnet_code_to_ticker(t.get('company_code'))
            if not ticker:
                continue
            pub = str(t.get('pubdate') or '')
            out.append({'id': str(t.get('id') or f"{ticker}-{pub}"), 'ticker': ticker, 'name': t.get('company_name'),
                        'title': t.get('title'), 'disc_date': pub[:10] or d.strftime('%Y-%m-%d'),
                        'disc_time': pub[11:16], 'url': t.get('document_url')})
        time.sleep(1.0)
    out.sort(key=lambda x: (x['disc_date'], x['disc_time'], x['ticker']))
    return out


# ---------------------------------------------------------------------------
# 2. 株価（Yahoo Finance）と取引可能フィルター
# ---------------------------------------------------------------------------

def fetch_missing_histories(tickers, period='9mo'):
    """collector.py が取得していない銘柄だけ Yahoo Finance から日足を取る。"""
    if not tickers:
        return {}
    import yfinance as yf
    out = {}
    for t in sorted(tickers):
        try:
            df = yf.Ticker(t).history(period=period, auto_adjust=True)
            if df is not None and not df.empty:
                df.index = pd.to_datetime(df.index).tz_localize(None)
                out[t] = df[['Open', 'High', 'Low', 'Close', 'Volume']].dropna(subset=['Close'])
        except Exception as e:  # noqa: BLE001
            print(f'[event] {t} の株価取得に失敗: {e}', file=sys.stderr)
        time.sleep(0.5)
    return out


def atr_series(df, period=14):
    prev = df['Close'].shift(1)
    tr = pd.concat([df['High'] - df['Low'], (df['High'] - prev).abs(), (df['Low'] - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def tradeable_flags(df, atr):
    value = df['Close'] * df['Volume']
    v20 = value.rolling(20, min_periods=20).mean()
    v250 = value.shift(20).rolling(250, min_periods=60).mean()
    hi60 = df['High'].rolling(60, min_periods=20).max()
    lo60 = df['Low'].rolling(60, min_periods=20).min()
    ok = (v20 >= MIN_AVG_TRADING_VALUE_YEN)
    ok &= ~((v250 > 0) & (v20 / v250 >= SURGE_VALUE_RATIO))
    ok &= ~((lo60 > 0) & (hi60 / lo60 >= PRICE_RANGE_RATIO_60))
    ok &= ~(atr / df['Close'] >= MAX_ATR_PCT)
    return ok.fillna(False)


def market_index(histories):
    """前日に取引可能だった銘柄の等金額平均の指数（日付→水準）と20日高値からの下落率。"""
    rets, eligs = [], []
    for t, df in histories.items():
        if df is None or len(df) < 30:
            continue
        a = atr_series(df)
        ok = tradeable_flags(df, a).shift(1, fill_value=False)
        r = df['Close'] / df['Close'].shift(1) - 1
        rets.append(r.rename(t))
        eligs.append(ok.rename(t))
    if not rets:
        return pd.DataFrame(columns=['level', 'dd20'])
    R = pd.concat(rets, axis=1).sort_index()
    E = pd.concat(eligs, axis=1).reindex(R.index).fillna(False).astype(bool)
    ew = R.where(E & R.notna()).mean(axis=1).fillna(0)
    level = (1 + ew).cumprod()
    return pd.DataFrame({'level': level, 'dd20': level / level.rolling(20, min_periods=1).max() - 1})


# ---------------------------------------------------------------------------
# 3. 記録（仮想ポジション）
# ---------------------------------------------------------------------------

def _load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f'[event] {path} の読み込みに失敗、初期状態で再開します: {e}', file=sys.stderr)
        return default


def _save_json(path, data):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _bars(df):
    return [(d.strftime('%Y-%m-%d'), float(o), float(h), float(l), float(c))
            for d, o, h, l, c in zip(df.index, df['Open'], df['High'], df['Low'], df['Close'])]


def _level_on_or_before(index_df, date):
    if index_df is None or index_df.empty:
        return None
    sub = index_df.loc[:pd.Timestamp(date)]
    return float(sub['level'].iloc[-1]) if len(sub) else None


def update_position(p, df, atr_now, index_df):
    """
    1ポジションを、まだ処理していない日足で進める（何度呼んでも同じ結果＝冪等）。
    p['state']: 'pending_entry' → 'open' →（'exit_next_open'）→ 'closed'
    """
    bars = [b for b in _bars(df) if b[0] > p['signal_date']]
    if not bars:
        return
    if p['state'] == 'pending_entry':
        d, o = bars[0][0], bars[0][1]
        if not o > 0:
            return
        a = p.get('atr_at_signal') or o * FALLBACK_ATR_PCT
        p.update(state='open', entry_date=d, entry_price=round(o, 2), held_days=0, last_processed=None)
        if p['series'] == DIVIDEND_HIKE:
            p['stop'] = round(o - a * DIVIDEND_TRAIL_ATR, 2)
        else:
            p['take_profit'] = round(o + a * CRASH_TP_ATR, 2)
            p['stop'] = round(o - a * CRASH_SL_ATR, 2)
    todo = [b for b in bars if b[0] >= p['entry_date'] and (p.get('last_processed') is None or b[0] > p['last_processed'])]
    for d, o, h, l, c in todo:
        if p['state'] == 'closed':
            break
        if p['state'] == 'exit_next_open':
            _close(p, d, o, '終値でストップに抵触→翌営業日の始値', index_df)
            break
        first_day = d == p['entry_date']
        if p['series'] == DIVIDEND_HIKE:
            a = atr_now if atr_now and atr_now > 0 else c * FALLBACK_ATR_PCT
            p['stop'] = round(max(p['stop'], c - a * DIVIDEND_TRAIL_ATR), 2)
            if c <= p['stop']:
                p['state'] = 'exit_next_open'
        else:
            p['held_days'] = p.get('held_days', 0) + 1
            tp, sl = p['take_profit'], p['stop']
            if not first_day and o <= sl:
                _close(p, d, o, '寄り付きで損切りラインを下回った（始値）', index_df)
            elif not first_day and o >= tp:
                _close(p, d, o, '寄り付きで利確ラインを上回った（始値）', index_df)
            elif l <= sl:
                _close(p, d, sl, '損切り', index_df)
            elif h >= tp:
                _close(p, d, tp, '利確', index_df)
            elif p['held_days'] >= CRASH_MAX_DAYS:
                _close(p, d, c, f'最長{CRASH_MAX_DAYS}営業日（終値）', index_df)
        p['last_processed'] = d
        if p['state'] != 'closed':
            p['last_price'], p['last_price_date'] = round(c, 2), d  # 含み損益の評価用（最新の終値）


def _close(p, date, price, reason, index_df):
    p.update(state='closed', exit_date=date, exit_price=round(float(price), 2), exit_reason=reason)
    ret = (price / p['entry_price'] - 1) * 100 - COST_PCT
    p['return_pct'] = round(ret, 2)
    a, b = p.get('market_level_at_signal'), _level_on_or_before(index_df, date)
    p['market_return_pct'] = round((b / a - 1) * 100, 2) if a and b else None
    p['excess_pct'] = round(ret - p['market_return_pct'], 2) if p['market_return_pct'] is not None else None


def _trading_days_between(df, d0, d1):
    idx = [x.strftime('%Y-%m-%d') for x in df.index]
    return sum(1 for x in idx if d0 < x <= d1)


def run(histories, today_utc=None, tdnet_items=None, fetch_missing=True):
    """
    collector.py から1日1回呼ぶ。histories: {ticker: 日足DataFrame}（Yahoo Finance）。
    戻り値：投稿用のサマリー dict（新規シグナル・暴落の発動・系列ごとの成績）。
    """
    today_utc = today_utc or datetime.now(timezone.utc)
    today_jst = today_utc.astimezone(JST)
    log = _load_json(EVENT_TRADE_LOG_PATH, [])
    state = _load_json(EVENT_STATE_PATH, {'seen_disclosure_ids': [], 'last_crash_trigger': None})

    # --- 増配修正（TDnet） ---
    if tdnet_items is None:
        tdnet_items = fetch_tdnet_dividend_hikes(today_jst)
    seen = set(state.get('seen_disclosure_ids', []))
    new_hikes = [x for x in tdnet_items if x['id'] not in seen]
    need = {x['ticker'] for x in new_hikes} | {p['ticker'] for p in log if p['state'] != 'closed'}
    missing = sorted(t for t in need if t not in histories)
    extra = fetch_missing_histories(missing) if fetch_missing else {}
    all_hist = {**histories, **extra}
    index_df = market_index(histories)

    opened_hikes = []
    for x in new_hikes:
        seen.add(x['id'])
        df = all_hist.get(x['ticker'])
        if df is None or len(df) < 30:
            continue
        atr = atr_series(df)
        sig_rows = df.loc[:pd.Timestamp(x['disc_date'])]
        if sig_rows.empty:
            continue
        sig_date = df.index[len(sig_rows) - 1].strftime('%Y-%m-%d')
        if pd.Timestamp(x['disc_date']) > df.index[len(sig_rows) - 1]:
            # 非営業日・当日分の足がまだ無い開示：その日（またはその次の営業日）の足ができてから判定する
            later = df.loc[pd.Timestamp(x['disc_date']):]
            if later.empty:
                seen.discard(x['id'])  # 次回やり直す
                continue
            sig_date = later.index[0].strftime('%Y-%m-%d')
        k = [d.strftime('%Y-%m-%d') for d in df.index].index(sig_date)
        if not bool(tradeable_flags(df, atr).iloc[max(0, k - 1)]):
            continue
        dup = [p for p in log if p['series'] == DIVIDEND_HIKE and p['ticker'] == x['ticker'] and
               (p['state'] != 'closed' or _trading_days_between(df, p['signal_date'], sig_date) < DEDUP_TRADING_DAYS)]
        if dup:
            continue
        a = float(atr.iloc[k]) if atr.iloc[k] == atr.iloc[k] else None
        p = {'series': DIVIDEND_HIKE, 'ticker': x['ticker'], 'name': x['name'], 'signal_date': sig_date,
             'reason': x['title'], 'disclosure_url': x['url'], 'atr_at_signal': round(a, 4) if a else None,
             'market_level_at_signal': _level_on_or_before(index_df, sig_date), 'state': 'pending_entry'}
        log.append(p)
        opened_hikes.append(p)
    state['seen_disclosure_ids'] = sorted(seen)[-5000:]

    # --- 暴落の発動 ---
    crash = None
    if not index_df.empty:
        last_date = index_df.index[-1].strftime('%Y-%m-%d')
        dd = float(index_df['dd20'].iloc[-1])
        last_trig = state.get('last_crash_trigger')
        cooled = last_trig is None or (index_df.index > pd.Timestamp(last_trig)).sum() >= CRASH_COOLDOWN_DAYS
        if dd <= CRASH_INDEX_DD and cooled:
            state['last_crash_trigger'] = last_date
            cands = []
            for t, df in histories.items():
                if df is None or len(df) < 30 or df.index[-1].strftime('%Y-%m-%d') != last_date:
                    continue
                atr = atr_series(df)
                if not bool(tradeable_flags(df, atr).iloc[-1]):
                    continue
                drop = float(df['Close'].iloc[-1] / df['Close'].iloc[-21:].max() - 1)
                if drop <= CRASH_STOCK_DD:
                    a = float(atr.iloc[-1])
                    p = {'series': CRASH_REBOUND, 'ticker': t, 'name': None, 'signal_date': last_date,
                         'reason': f'指数が20日高値から{dd * 100:.1f}%／この銘柄は{drop * 100:.1f}%',
                         'atr_at_signal': round(a, 4), 'drop_pct': round(drop * 100, 1),
                         'market_level_at_signal': _level_on_or_before(index_df, last_date), 'state': 'pending_entry'}
                    log.append(p)
                    cands.append(p)
            crash = {'date': last_date, 'index_dd_pct': round(dd * 100, 2),
                     'candidates': sorted(cands, key=lambda p: p['drop_pct'])}
        state['index_last'] = {'date': last_date, 'dd20_pct': round(dd * 100, 2)}

    # --- 既存ポジションの更新 ---
    for p in log:
        if p['state'] == 'closed':
            continue
        df = all_hist.get(p['ticker'])
        if df is None or df.empty:
            continue
        atr = atr_series(df)
        update_position(p, df, float(atr.iloc[-1]) if atr.iloc[-1] == atr.iloc[-1] else None, index_df)

    _save_json(EVENT_TRADE_LOG_PATH, log)
    _save_json(EVENT_STATE_PATH, state)
    return {'new_dividend_hikes': opened_hikes, 'crash': crash, 'performance': performance(log),
            'index': state.get('index_last')}


def performance(log):
    out = {}
    for series in (DIVIDEND_HIKE, CRASH_REBOUND):
        ps = [p for p in log if p['series'] == series]
        closed = [p for p in ps if p['state'] == 'closed']
        rets = [p['return_pct'] for p in closed]
        wins = [r for r in rets if r > 0]
        losses = [r for r in rets if r <= 0]
        ex = [p['excess_pct'] for p in closed if p.get('excess_pct') is not None]
        # 【2026-09-22追加】保有中の評価（1銘柄100株換算）と、決済済みの確定損益
        lot = VALUATION_LOT_SHARES
        opens = [p for p in ps if p['state'] in ('open', 'exit_next_open') and p.get('entry_price')]
        principal = sum(p['entry_price'] * lot for p in opens)
        value = sum((p.get('last_price') or p['entry_price']) * lot for p in opens)
        realized = sum(p['entry_price'] * lot * p['return_pct'] / 100 for p in closed if p.get('return_pct') is not None)
        realized_principal = sum(p['entry_price'] * lot for p in closed if p.get('return_pct') is not None)
        out[series] = {
            'label': SERIES_LABEL[series], 'closed': len(closed),
            'open': sum(1 for p in ps if p['state'] in ('open', 'exit_next_open')),
            'pending_entry': sum(1 for p in ps if p['state'] == 'pending_entry'),
            'win_rate_pct': round(len(wins) / len(rets) * 100, 1) if rets else None,
            'payoff_ratio': round((sum(wins) / len(wins)) / abs(sum(losses) / len(losses)), 2) if wins and losses and sum(losses) else None,
            'expectancy_pct': round(sum(rets) / len(rets), 2) if rets else None,
            'excess_pct': round(sum(ex) / len(ex), 2) if ex else None,
            'lot_shares': lot,
            'principal': round(principal), 'value': round(value), 'unrealized': round(value - principal),
            'unrealized_pct': round((value - principal) / principal * 100, 2) if principal else None,
            'realized': round(realized),
            'realized_pct': round(realized / realized_principal * 100, 2) if realized_principal else None,
        }
    return out
