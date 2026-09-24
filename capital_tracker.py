#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
capital_tracker.py — 「500万円を元手に、この日から実際にbotのシグナル通り運用していたら」
を再現する資金台帳。data/trade_log.json（本番の仮想シグナル追跡）を日次で読み、新規エントリー
の資金配分・決済時の確定損益・保有中ポジションの含み損益（現在値ベースの時価評価）を
data/capital_ledger.json に積み上げていく。

【前提（2026-09-24、ユーザーとの合意事項）】
- 初期資金: 5,000,000円
- 1トレードあたりの投資額: その時点のequity（現金＋保有中ポジションの簿価）の10%（複利）
- 同時保有の上限: 投資済み合計がequityの80%を超える新規エントリーは資金不足で見送る
  （10%×8=80%なので、実質「最大8ポジション」に相当）
- 対象は現物LONGのみ（信用取引・SHORTは対象外。SHORTシグナルは実際の空売り対象ではなく、
  保有中のLONG銘柄の判定が「今日SHORT」に変わった場合の警戒シグナルとしてのみ使う
  ＝apply_caution_flags参照。自動決済はしない、注意喚起の表示のみ）
- 対象は「この台帳の開始日（start_date）以降に新規エントリーしたポジション」のみ。
  過去分（2026-08-25の一括ロード分やそれ以前の検証用エントリー）は含めない
  （ユーザー要望：「これからは新規でポジションをとるものに関しては」）。
- 決済判定・トレーリングストップはtracking.py本番ロジック（ATR×3.0倍、LONGプライマリ。
  `ATR_MULTIPLIER_BY_SIGNAL`参照）を
  そのまま使う。本ファイルは「決済されたかどうか」をtrade_log.json側の結果から読み取り、
  資金の出入りだけを追加で計算する（判定ロジックの二重実装はしない）。
- 含み損益の時価評価には、tracking.update_open_positions()が毎日更新している
  p['last_price']（当日収集できなければ直近の既知終値）を使う。
- 年が変わったら台帳を500万円・保有0件にリセットする（ユーザー要望・2026-09-24合意）。
  年をまたいで保有中だったポジションは、年末時点の時価で「注記上の決済」として前年分
  （data/capital_ledger_archive/<年>.json）に含め、新年の台帳では追跡を打ち切る
  （rollover_if_new_year参照。bot本体のtrade_log.json側の追跡とは独立）。

【運用方法】
  python capital_tracker.py
  収集ワークフロー（collect_data.yml）でcollector.py実行後に呼ぶことを想定。
  複数回実行しても安全（冪等）：既に処理済みのエントリー・決済は二重計上しない。
"""

import json
import os
from datetime import datetime

from tracking import (
    LEGACY_BULK_LOAD_ENTRY_DATES,
    PRIMARY_VARIANT_KEY_BY_SIGNAL,
    load_trade_log,
)

LATEST_SCAN_PATH = os.getenv('LATEST_SCAN_PATH') or 'data/latest_scan.json'
LEDGER_PATH = os.getenv('CAPITAL_LEDGER_PATH') or 'data/capital_ledger.json'
INITIAL_CAPITAL = float(os.getenv('CAPITAL_TRACKER_INITIAL', '5000000'))
PER_TRADE_FRACTION = float(os.getenv('CAPITAL_TRACKER_PER_TRADE_FRACTION', '0.10'))
MAX_DEPLOYED_FRACTION = float(os.getenv('CAPITAL_TRACKER_MAX_DEPLOYED_FRACTION', '0.80'))
SIGNAL_FILTER = ('LONG',)  # 現物のみ想定のためSHORTは対象外


def _position_key(p):
    return f"{p['ticker']}|{p['signal']}|{p['entry_date']}"


def _new_ledger(start_date):
    return {
        'initial_capital': INITIAL_CAPITAL,
        'per_trade_fraction': PER_TRADE_FRACTION,
        'max_deployed_fraction': MAX_DEPLOYED_FRACTION,
        'signal_filter': list(SIGNAL_FILTER),
        'start_date': start_date,
        'period_year': int(start_date[:4]),
        'cash': INITIAL_CAPITAL,
        'open_positions': {},   # key -> {ticker, name, signal, entry_date, entry_price, invested, shares}
        'closed_trades': [],    # 確定済み
        'skipped_entries': [],  # 資金不足で見送った候補
        'last_synced_at': None,
    }


def load_ledger(start_date=None):
    if os.path.exists(LEDGER_PATH):
        with open(LEDGER_PATH, encoding='utf-8') as f:
            ledger = json.load(f)
        if 'period_year' not in ledger:  # 年越しリセット機能追加前に作った台帳との後方互換
            ledger['period_year'] = int(ledger['start_date'][:4])
        return ledger
    if start_date is None:
        start_date = datetime.now().strftime('%Y-%m-%d')
    return _new_ledger(start_date)


def save_ledger(ledger):
    out_dir = os.path.dirname(LEDGER_PATH)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    tmp = LEDGER_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(ledger, f, ensure_ascii=False, indent=2)
    os.replace(tmp, LEDGER_PATH)


def _realize_close(ledger, key, pos, primary):
    invested = pos['invested']
    return_pct = primary['return_pct']
    proceeds = invested * (1 + return_pct / 100)
    pnl = proceeds - invested
    ledger['cash'] += proceeds
    ledger['closed_trades'].append({
        'ticker': pos['ticker'], 'name': pos.get('name'), 'signal': pos['signal'],
        'entry_date': pos['entry_date'], 'entry_price': pos['entry_price'],
        'close_date': primary.get('close_date'), 'close_price': primary.get('close_price'),
        'invested': round(invested, 0), 'pnl': round(pnl, 0), 'return_pct': return_pct,
    })
    del ledger['open_positions'][key]


def _mark_to_market(pos, rec):
    """
    posの含み損益を、trade_log側の最新レコード(rec)が持つlast_priceで時価更新する。
    last_priceが無い（当日収集で一度もupdate_open_positionsを通っていない）場合は
    entry_priceを仮の代用にはせず、価格不明（unrealized=None）のまま明示する
    （数値を確定させてしまうと「含み損益0円」に見えてしまい、実際には値動きがある
    のに無かったことにしてしまう＝ユーザー要望の「含み益・含み損がわかるように」に反する）。
    """
    last_price = rec.get('last_price')
    pos['last_price_date'] = rec.get('last_price_date')
    if not last_price:
        pos['last_price'] = None
        pos['unrealized'] = None
        pos['unrealized_pct'] = None
        return
    entry = pos['entry_price']
    unrealized_pct = (last_price - entry) / entry * 100 if pos['signal'] == 'LONG' \
        else (entry - last_price) / entry * 100
    pos['last_price'] = last_price
    pos['unrealized'] = round(pos['invested'] * unrealized_pct / 100, 0)
    pos['unrealized_pct'] = round(unrealized_pct, 2)


ARCHIVE_DIR = os.getenv('CAPITAL_LEDGER_ARCHIVE_DIR') or 'data/capital_ledger_archive'


def rollover_if_new_year(ledger, trade_log, today_str):
    """
    年が変わっていたら、保有中ポジションを年末時価で「注記上の決済」として前年分に
    含めてアーカイブし（data/capital_ledger_archive/<年>.json）、新年の台帳を
    500万円・保有0件から作り直す（ユーザー要望：「年始になったらまた500万から
    スタート」。年をまたいだポジションは新年の台帳では引き継がず追跡を打ち切る、
    という単純な仕様で合意済み・2026-09-24）。
    実際のbot側（trade_log.json）のポジションはこれとは無関係にそのまま追跡が続く
    （本関数はcapital_tracker.py独自の「資金の帳簿」だけをリセットする）。
    """
    current_year = int(today_str[:4])
    period_year = ledger.get('period_year') or int(ledger['start_date'][:4])
    if current_year <= period_year:
        return ledger

    by_key = {_position_key(t): t for t in trade_log}
    for key, pos in list(ledger['open_positions'].items()):
        rec = by_key.get(key)
        if rec is not None:
            _mark_to_market(pos, rec)
        unrealized = pos.get('unrealized') or 0
        proceeds = pos['invested'] + unrealized
        ledger['cash'] += proceeds
        ledger['closed_trades'].append({
            'ticker': pos['ticker'], 'name': pos.get('name'), 'signal': pos['signal'],
            'entry_date': pos['entry_date'], 'entry_price': pos['entry_price'],
            'close_date': f'{period_year}-12-31', 'close_price': pos.get('last_price'),
            'invested': pos['invested'], 'pnl': round(unrealized, 0),
            'return_pct': pos.get('unrealized_pct'),
            'close_reason': 'year_end_rollover',
        })
        del ledger['open_positions'][key]

    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    ledger['final_equity'] = round(ledger['cash'], 0)
    ledger['rolled_over_at'] = today_str
    archive_path = os.path.join(ARCHIVE_DIR, f'{period_year}.json')
    with open(archive_path, 'w', encoding='utf-8') as f:
        json.dump(ledger, f, ensure_ascii=False, indent=2)
    print(f'[capital_tracker] {period_year}年分を{archive_path}にアーカイブしました'
          f'（年末時点の資産: {ledger["final_equity"]:,.0f}円）。{current_year}年の台帳を500万円で開始します。')

    return _new_ledger(f'{current_year}-01-01')


def sync(ledger, trade_log, today_str):
    by_key = {_position_key(t): t for t in trade_log}

    # 1) 既存の保有中ポジション: 決済されていれば確定させ、まだ保有中なら含み損益を時価更新
    for key in list(ledger['open_positions'].keys()):
        pos = ledger['open_positions'][key]
        rec = by_key.get(key)
        if rec is None:
            continue  # trade_log側に見つからない（想定外）。次回まで保留
        primary_key = PRIMARY_VARIANT_KEY_BY_SIGNAL[pos['signal']]
        primary = (rec.get('variants') or {}).get(primary_key) or {}
        if primary.get('status') == 'closed' and primary.get('return_pct') is not None:
            _realize_close(ledger, key, pos, primary)
        else:
            _mark_to_market(pos, rec)

    # 2) 新規エントリー候補（台帳開始日以降・LONGのみ・レガシー一括分は除外・未処理のもの）
    already_seen = set(ledger['open_positions'].keys()) \
        | {f"{t['ticker']}|{t['signal']}|{t['entry_date']}" for t in ledger['closed_trades']} \
        | set(ledger['skipped_entries'])
    candidates = [
        t for t in trade_log
        if t.get('signal') in SIGNAL_FILTER
        and t.get('entry_date', '') >= ledger['start_date']
        and t.get('entry_date') not in LEGACY_BULK_LOAD_ENTRY_DATES
        and _position_key(t) not in already_seen
    ]
    candidates.sort(key=lambda t: (t['entry_date'], t['ticker']))

    for rec in candidates:
        key = _position_key(rec)
        entry_price = rec.get('entry_price')
        if not entry_price or entry_price <= 0:
            continue
        invested_total = sum(p['invested'] for p in ledger['open_positions'].values())
        equity = ledger['cash'] + invested_total
        position_size = equity * ledger['per_trade_fraction']
        room = equity * ledger['max_deployed_fraction'] - invested_total
        if position_size > room or position_size > ledger['cash']:
            ledger['skipped_entries'].append(key)
            continue

        ledger['cash'] -= position_size
        new_pos = {
            'ticker': rec['ticker'], 'name': rec.get('name'), 'signal': rec['signal'],
            'entry_date': rec['entry_date'], 'entry_price': entry_price,
            'invested': round(position_size, 0), 'shares': round(position_size / entry_price, 4),
        }
        ledger['open_positions'][key] = new_pos
        _mark_to_market(new_pos, rec)  # 同期の間隔が空いていた場合に備え、追加した時点の最新値ですぐ時価評価する

        # その場で既に決済済みだった場合（同期の間隔が空いた場合）はそのまま確定させる
        primary_key = PRIMARY_VARIANT_KEY_BY_SIGNAL[rec['signal']]
        primary = (rec.get('variants') or {}).get(primary_key) or {}
        if primary.get('status') == 'closed' and primary.get('return_pct') is not None:
            _realize_close(ledger, key, ledger['open_positions'][key], primary)

    ledger['last_synced_at'] = today_str
    return ledger


def summarize(ledger):
    open_positions = list(ledger['open_positions'].values())
    invested_book = sum(p['invested'] for p in open_positions)
    price_unknown = [p for p in open_positions if p.get('unrealized') is None]
    unrealized_total = sum(p.get('unrealized') or 0 for p in open_positions)
    market_value = invested_book + unrealized_total
    equity = ledger['cash'] + market_value
    realized_total = sum(t['pnl'] for t in ledger['closed_trades'])

    wins = [t for t in ledger['closed_trades'] if t['return_pct'] > 0]
    return {
        'start_date': ledger['start_date'],
        'initial_capital': ledger['initial_capital'],
        'cash': round(ledger['cash'], 0),
        'open_count': len(open_positions),
        'invested_book': round(invested_book, 0),
        'unrealized_pnl': round(unrealized_total, 0),
        'market_value': round(market_value, 0),
        'realized_pnl': round(realized_total, 0),
        'closed_count': len(ledger['closed_trades']),
        'win_rate_pct': round(len(wins) / len(ledger['closed_trades']) * 100, 1) if ledger['closed_trades'] else None,
        'skipped_count': len(ledger['skipped_entries']),
        'price_unknown_count': len(price_unknown),
        'equity': round(equity, 0),
        'total_pnl': round(equity - ledger['initial_capital'], 0),
        'return_pct': round((equity / ledger['initial_capital'] - 1) * 100, 2),
    }


def load_latest_scan_signals(path=None):
    """
    当日の判定結果（data/latest_scan.json の'results'）から、銘柄ごとの最新シグナルを返す。
    見つからなければ空辞書（呼び出し側は「警戒判定できず」として扱う）。
    """
    path = path or LATEST_SCAN_PATH
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding='utf-8') as f:
            scan = json.load(f)
    except Exception:
        return {}
    return {t: r.get('signal') for t, r in (scan.get('results') or {}).items()}


def apply_caution_flags(ledger, signals):
    """
    保有中の現物LONGポジションについて、その銘柄の「今日の」判定がSHORTだった場合に
    警戒フラグを立てる（ユーザー方針：ショートは実際に空売りする対象ではなく、
    保有銘柄にとっては「弱気の目安が点灯した」という警戒シグナルとして使う）。
    ポジション自体のトレーリングストップは変えない（自動決済しない、あくまで注意喚起）。
    """
    for pos in ledger['open_positions'].values():
        pos['caution'] = signals.get(pos['ticker']) == 'SHORT'
    return ledger


def print_report(ledger):
    s = summarize(ledger)
    print('=' * 64)
    print(f"資金台帳（{s['start_date']}開始・現物LONGのみ・複利10%・最大投入80%）")
    print('=' * 64)
    print(f"初期資金: {s['initial_capital']:,.0f}円")
    print(f"現在の資産評価額: {s['equity']:,.0f}円（{s['total_pnl']:+,.0f}円 / {s['return_pct']:+.2f}%）")
    print(f"  内訳: 現金 {s['cash']:,.0f}円 + 保有中ポジション時価 {s['market_value']:,.0f}円"
          f"（元本{s['invested_book']:,.0f}円・含み損益{s['unrealized_pnl']:+,.0f}円）")
    print(f"確定損益（累計）: {s['realized_pnl']:+,.0f}円（決済{s['closed_count']}件"
          + (f"・勝率{s['win_rate_pct']}%" if s['win_rate_pct'] is not None else '') + '）')
    print(f"保有中ポジション: {s['open_count']}件 / 資金不足で見送り: {s['skipped_count']}件"
          + (f" / 価格不明: {s['price_unknown_count']}件" if s['price_unknown_count'] else ''))
    if ledger['open_positions']:
        print('-' * 64)
        for p in sorted(ledger['open_positions'].values(), key=lambda x: x['entry_date']):
            if p.get('unrealized') is None:
                pnl_str = '含み損益 価格取得できず不明'
            else:
                pnl_str = f"含み{p['unrealized']:+,.0f}円({p['unrealized_pct']:+.2f}%)"
            caution_str = ' ⚠️警戒(本日SHORT判定)' if p.get('caution') else ''
            print(f"  {p['ticker']} {p.get('name') or ''} {p['signal']} "
                  f"entry {p['entry_date']}@{p['entry_price']:,.0f} 元本{p['invested']:,.0f}円 {pnl_str}{caution_str}")
        caution_n = sum(1 for p in ledger['open_positions'].values() if p.get('caution'))
        if caution_n:
            print(f"\n⚠️ {caution_n}件が本日SHORT判定（弱気シグナル点灯中・保有継続の可否は要確認）")


def main():
    trade_log = load_trade_log()
    ledger = load_ledger()
    today_str = datetime.now().strftime('%Y-%m-%d')
    ledger = rollover_if_new_year(ledger, trade_log, today_str)
    sync(ledger, trade_log, today_str)
    apply_caution_flags(ledger, load_latest_scan_signals())
    save_ledger(ledger)
    print_report(ledger)


if __name__ == '__main__':
    main()
