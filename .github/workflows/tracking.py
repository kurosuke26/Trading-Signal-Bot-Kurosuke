#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tracking.py — LONG/SHORTシグナルの「シグナル通りに売買していたら」の
仮想ポジション追跡・成績集計（勝率・ペイオフレシオ）。

【設計方針】
実際の取引記録ではなく、「その日にbotが出したLONG/SHORTシグナル通りに
エントリーし、scoring.suggested_trade_levels() と同じATR×1.5のトレーリング
ストップルールで決済していたら、どうなっていたか」を collector.py の
日次収集のたびに自動シミュレーションし、data/trade_log.json に蓄積する。

- 同一銘柄・同一方向（LONG/SHORT）で既に建玉中（open）のポジションがあれば、
  新規シグナルが出ても二重にエントリーしない（既存ポジションのトレーリング
  ストップ更新のみ行う）。
- 決済（close）は、その日の終値がATR×1.5のトレーリングストップに抵触した
  ものとして扱う（シャンデリア・ストップ方式。LONGは切り上げのみ、
  SHORTは切り下げのみ）。この処理は1日1回（deep collectorの実行時）しか
  行われないため、「ストップ価格ちょうどで約定した」のではなく「抵触を検知した
  その日の終値で手仕舞いした」という、日次バッチとしての実態に即した保守的な
  シミュレーションになっている（intraday（日中）の実際のストップ注文約定とは
  異なり、ギャップ（窓開け）が大きい日は損益が理論上のストップ幅より
  大きくぶれ得る）。
- 集計は「LONG＋SHORT合算」と「LONGのみ」の2系統を別々に算出する
  （ユーザーの要望：今回の集大成として両方のケースを見たいとのこと）。

【正直な注意点】
これは実際の売買記録ではなく、シグナル通りに機械的に売買した場合の
シミュレーションです。実際にどの銘柄を選んで取引するかはユーザー次第
であり、この勝率・ペイオフレシオはあくまで「シグナルそのものの成績」の
目安であって、ユーザー個人の実績ではありません。また、収集開始直後は
決済済みの取引が無いため「集計中」と表示されます。統計として意味のある
件数が貯まるまでには数週間程度かかる見込みです。
"""

import json
import os

from util import json_default

TRADE_LOG_PATH = os.getenv('TRADE_LOG_PATH') or 'data/trade_log.json'
ATR_MULTIPLIER = 1.5
FALLBACK_STOP_PCT = 0.03  # ATRが算出できない銘柄向けの簡易フォールバック（±3%）


def load_trade_log(path=None):
    """
    【注意】write_snapshot/load_snapshotと同様、デフォルト引数を
    path=TRADE_LOG_PATH とすると関数定義時点の値に固定されてしまうため、
    path=None にして呼び出し時にモジュールグローバルを参照する形にしている。
    """
    if path is None:
        path = TRADE_LOG_PATH
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        # 壊れたファイルで全処理が止まらないよう、空リストから再出発する
        return []


def save_trade_log(trade_log, path=None):
    if path is None:
        path = TRADE_LOG_PATH
    out_dir = os.path.dirname(path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    tmp_path = path + '.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(trade_log, f, ensure_ascii=False, default=json_default)
    os.replace(tmp_path, path)


def _has_open(trade_log, ticker, signal):
    return any(p['ticker'] == ticker and p['signal'] == signal and p['status'] == 'open' for p in trade_log)


def _initial_stop(entry_price, atr_value, signal, multiplier=ATR_MULTIPLIER):
    if atr_value is None or atr_value <= 0:
        atr_value = entry_price * FALLBACK_STOP_PCT
    if signal == 'LONG':
        return entry_price - atr_value * multiplier
    return entry_price + atr_value * multiplier  # SHORT


def open_new_positions(trade_log, results, today_str):
    """
    今回の収集でLONG/SHORT判定になった銘柄のうち、まだ建玉中(open)のものが
    無い銘柄について、新規の仮想ポジションを1件開く。戻り値は新規開設件数。
    """
    opened = 0
    for ticker, r in results.items():
        signal = r.get('signal')
        if signal not in ('LONG', 'SHORT'):
            continue
        if _has_open(trade_log, ticker, signal):
            continue
        entry_price = r.get('current_price')
        if not entry_price or entry_price <= 0:
            continue
        atr_value = (r.get('tech_snapshot') or {}).get('atr')
        stop = _initial_stop(entry_price, atr_value, signal)
        trade_log.append({
            'ticker': ticker,
            'name': r.get('name'),
            'signal': signal,
            'entry_date': today_str,
            'entry_price': round(entry_price, 2),
            'stop': round(stop, 2),
            'status': 'open',
            'close_date': None,
            'close_price': None,
            'return_pct': None,
        })
        opened += 1
    return opened


def update_open_positions(trade_log, results, today_str):
    """
    建玉中の全ポジションについて、今回取得できた最新価格・ATRでトレーリング
    ストップを更新し、抵触していれば決済（close）する。戻り値は決済件数。

    今回データ取得に失敗した銘柄（resultsに存在しない）は判定をスキップし、
    次回の収集時にあらためて判定する（データ欠損による誤決済を避けるため）。
    """
    closed = 0
    for p in trade_log:
        if p['status'] != 'open':
            continue
        r = results.get(p['ticker'])
        if r is None:
            continue
        current_price = r.get('current_price')
        if not current_price or current_price <= 0:
            continue
        atr_value = (r.get('tech_snapshot') or {}).get('atr')
        if atr_value is None or atr_value <= 0:
            atr_value = current_price * FALLBACK_STOP_PCT

        if p['signal'] == 'LONG':
            candidate_stop = current_price - atr_value * ATR_MULTIPLIER
            new_stop = max(p['stop'], candidate_stop)  # 切り上げのみ
            hit = current_price <= new_stop
        else:  # SHORT
            candidate_stop = current_price + atr_value * ATR_MULTIPLIER
            new_stop = min(p['stop'], candidate_stop)  # 切り下げのみ
            hit = current_price >= new_stop

        p['stop'] = round(new_stop, 2)

        if hit:
            entry = p['entry_price']
            if p['signal'] == 'LONG':
                ret_pct = (current_price - entry) / entry * 100
            else:
                ret_pct = (entry - current_price) / entry * 100
            p['status'] = 'closed'
            p['close_date'] = today_str
            p['close_price'] = round(current_price, 2)
            p['return_pct'] = round(ret_pct, 2)
            closed += 1
    return closed


def _stats_for(closed_trades):
    n = len(closed_trades)
    if n == 0:
        return {
            'closed_count': 0, 'win_count': 0, 'win_rate': None,
            'avg_win_pct': None, 'avg_loss_pct': None, 'payoff_ratio': None,
        }
    wins = [t['return_pct'] for t in closed_trades if t['return_pct'] is not None and t['return_pct'] > 0]
    losses = [t['return_pct'] for t in closed_trades if t['return_pct'] is not None and t['return_pct'] <= 0]
    win_rate = round(len(wins) / n * 100, 1)
    avg_win = round(sum(wins) / len(wins), 2) if wins else None
    avg_loss = round(sum(losses) / len(losses), 2) if losses else None
    payoff = None
    if avg_win is not None and avg_loss is not None and avg_loss != 0:
        payoff = round(avg_win / abs(avg_loss), 2)
    return {
        'closed_count': n, 'win_count': len(wins), 'win_rate': win_rate,
        'avg_win_pct': avg_win, 'avg_loss_pct': avg_loss, 'payoff_ratio': payoff,
    }


def compute_performance_stats(trade_log):
    """勝率・ペイオフレシオを「LONG＋SHORT合算」「LONGのみ」の2系統で算出する。"""
    closed_all = [p for p in trade_log if p['status'] == 'closed']
    closed_long = [p for p in closed_all if p['signal'] == 'LONG']
    open_count = len([p for p in trade_log if p['status'] == 'open'])

    return {
        'long_short': _stats_for(closed_all),
        'long_only': _stats_for(closed_long),
        'open_positions': open_count,
    }
