#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backtest.py — J-Quantsの過去データに対して、現行のLONG/SHORT判定・ATRトレーリングストップ
ロジックを日次で再現し、勝率・ペイオフレシオを検証する（Phase 1：小規模テスト）。

【設計方針：本番ロジックを「再実装」せず「再利用」する】
LONG/SHORT判定（collector.analyze_ticker）と、追跡・決済シミュレーション
（tracking.open_new_positions / update_open_positions / compute_performance_stats）は、
本番の`collector.py`・`tracking.py`をそのままimportして使う。バックテスト側で新しく
書いているのは「J-Quantsのキャッシュデータから、本番と同じ形の`fund`辞書・価格DataFrame・
日次ループを組み立てる」部分だけ。ロジックを二重実装すると本番との食い違い（バグ）が
生まれやすいため、この構成を最優先にしている。

【ファンダメンタルズの近似についての正直な注意点】
- PER・PBR・配当利回りは、J-Quants財務情報API（fins/summary）のEPS/BPS/DivAnnフィールドを
  使って計算する。EPSは開示ごとの累積値（四半期別）だが、本スクリプトは「BPSが埋まっている
  開示＝本決算（期末）開示」とみなし、そこでのEPS/BPS/DivAnnのみを採用してforward-fill
  している（四半期の累積EPSをそのままPERの分母に使うと年率換算がずれるため）。この仮定は
  Phase 1で実データを見て要検証（`_debug_dump_sample`で確認したサンプルと突き合わせること）。
- EPS3期推移（eps_trend）とグロース（growth_raw）は、今回のバックテスト対象から除外する
  （ユーザーとの合意事項、2026-09-04）。analyze_ticker呼び出し時は本番同様eps_trend=None
  固定・growth_rawもNone固定になる。
- 株価はraw（分割未調整）のO/H/L/C/Voを使用する（本番のyfinance取得もauto_adjust=Falseで
  raw値を使っているため、これに合わせている）。期間中に株式分割があった銘柄は、分割前後で
  価格が不連続になりATR・移動平均が乱れる可能性がある点に注意。

【実行方法】
  python backtest.py
  （事前に backfill_jquants.py でキャッシュを作成しておくこと）
"""

import json
import os
import sys
from datetime import datetime

import pandas as pd

from collector import analyze_ticker
import tracking

CACHE_DIR = os.getenv('JQUANTS_CACHE_DIR') or 'data/jquants_cache'
OUT_DIR = os.getenv('BACKTEST_OUT_DIR') or 'data/backtest_out'
BACKTEST_TRADE_LOG_PATH = os.path.join(OUT_DIR, 'backtest_trade_log.json')
BACKTEST_LOOKBACK_ROWS = int(os.getenv('BACKTEST_LOOKBACK_ROWS', '220'))  # 本番の9mo(≒190営業日)相当+余裕


def load_universe():
    meta_path = os.path.join(CACHE_DIR, '_universe.json')
    if not os.path.exists(meta_path):
        raise RuntimeError(f'{meta_path} が見つかりません。先に backfill_jquants.py を実行してください。')
    with open(meta_path, encoding='utf-8') as f:
        meta = json.load(f)
    return meta['universe']


def load_bars_df(ticker4):
    """bars.jsonを読み込み、本番と同じ列名（Open/High/Low/Close/Volume）のDataFrameにする。"""
    path = os.path.join(CACHE_DIR, ticker4, 'bars.json')
    if not os.path.exists(path):
        return None
    with open(path, encoding='utf-8') as f:
        rows = json.load(f)
    if not rows:
        return None

    df = pd.DataFrame(rows)
    rename_map = {'O': 'Open', 'H': 'High', 'L': 'Low', 'C': 'Close', 'Vo': 'Volume'}
    missing = [c for c in rename_map if c not in df.columns]
    if missing:
        print(f'[backtest] {ticker4}: bars.jsonに想定カラム{missing}が無く読み込めません。'
              'jquants_client.pyのフィールド名を実レスポンスと突き合わせて修正してください。',
              file=sys.stderr)
        return None
    df = df.rename(columns=rename_map)
    df['Date'] = pd.to_datetime(df['Date'])
    df = df.sort_values('Date').set_index('Date')
    for col in ['Open', 'High', 'Low', 'Close', 'Volume']:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    df = df.dropna(subset=['Close'])
    return df


def load_fins_timeline(ticker4):
    """
    fins.jsonを読み込み、「本決算（BPSが埋まっている）開示」だけを開示日順に並べたリストを返す。
    各要素: {'date': pd.Timestamp, 'eps': float or None, 'bps': float or None, 'div_ann': float or None}
    """
    path = os.path.join(CACHE_DIR, ticker4, 'fins.json')
    if not os.path.exists(path):
        return []
    with open(path, encoding='utf-8') as f:
        rows = json.load(f)

    def _num(v):
        try:
            if v is None or v == '':
                return None
            return float(v)
        except (TypeError, ValueError):
            return None

    out = []
    for r in rows:
        bps = _num(r.get('BPS'))
        if bps is None:
            continue  # BPS欠損＝四半期の中間開示とみなし、forward-fill元からは除外
        disc_date = r.get('DiscDate')
        if not disc_date:
            continue
        out.append({
            'date': pd.Timestamp(disc_date),
            'eps': _num(r.get('EPS')),
            'bps': bps,
            'div_ann': _num(r.get('DivAnn')),
        })
    out.sort(key=lambda x: x['date'])
    return out


def fundamentals_as_of(fins_timeline, date):
    """指定日時点で「知り得た」直近の本決算開示（EPS/BPS/DivAnn）を返す。無ければ全てNone。"""
    latest = None
    for rec in fins_timeline:
        if rec['date'] <= date:
            latest = rec
        else:
            break
    if latest is None:
        return None, None, None
    return latest['eps'], latest['bps'], latest['div_ann']


def build_fund(name, close_price, eps, bps, div_ann, sector_code=None, sector_name=None):
    per = (close_price / eps) if (eps and eps > 0 and close_price) else None
    pbr = (close_price / bps) if (bps and bps > 0 and close_price) else None
    div_pct = (div_ann / close_price * 100) if (div_ann and div_ann > 0 and close_price) else 0
    return {
        'name': name,
        'current_price': close_price,
        'dividend_yield': div_pct,
        'pbr': pbr or 0,
        'per': per or 0,
        'growth_raw': None,
        'sector': None,
        # 【2026-09-07追加】ATR倍率の業種別最適化分析用。_universe.jsonに業種コードが
        # 無い（=backfill_jquants.pyをJPX業種コード対応前に生成した）場合はNoneのまま。
        'sector_code': sector_code,
        'sector_name': sector_name,
    }


def run_backtest():
    universe = load_universe()
    print(f'[backtest] 対象ユニバース: {len(universe)}銘柄')

    tickers_data = {}
    for u in universe:
        t4 = u['ticker4']
        bars = load_bars_df(t4)
        if bars is None or bars.empty:
            print(f'[backtest] {t4}: 株価データなし、スキップ')
            continue
        fins_tl = load_fins_timeline(t4)
        tickers_data[t4] = {
            'name': u.get('name') or t4, 'bars': bars, 'fins': fins_tl,
            'sector_code': u.get('sector_code'), 'sector_name': u.get('sector_name'),
        }

    if not tickers_data:
        print('[backtest] 有効なデータが無いため終了します（backfill_jquants.pyの実行結果を確認してください）')
        return

    all_dates = sorted(set().union(*[set(d['bars'].index) for d in tickers_data.values()]))
    print(f'[backtest] シミュレーション対象日数: {len(all_dates)}日'
          f'（{all_dates[0].date()} 〜 {all_dates[-1].date()}）')

    os.makedirs(OUT_DIR, exist_ok=True)
    trade_log = tracking.load_trade_log(path=BACKTEST_TRADE_LOG_PATH)

    for di, date in enumerate(all_dates):
        results = {}
        for ticker4, d in tickers_data.items():
            bars = d['bars']
            if date not in bars.index:
                continue  # その日データが無い銘柄はスキップ（本番のfund_failed相当）
            df_upto = bars.loc[:date].tail(BACKTEST_LOOKBACK_ROWS)
            close_price = float(df_upto['Close'].iloc[-1])

            eps, bps, div_ann = fundamentals_as_of(d['fins'], date)
            fund = build_fund(d['name'], close_price, eps, bps, div_ann,
                               sector_code=d.get('sector_code'), sector_name=d.get('sector_name'))

            try:
                results[ticker4] = analyze_ticker(ticker4, fund, df_upto)
            except Exception as e:
                print(f'[backtest] {date.date()} {ticker4}: analyze_tickerでエラー: {e}', file=sys.stderr)

        today_str = date.strftime('%Y-%m-%d')
        closed_n = tracking.update_open_positions(trade_log, results, today_str)
        opened_n = tracking.open_new_positions(trade_log, results, today_str)

        if (di + 1) % 20 == 0 or di == len(all_dates) - 1:
            print(f'[backtest] 進捗 {di + 1}/{len(all_dates)}日目（{today_str}）'
                  f' 新規{opened_n}件／決済{closed_n}件／ログ累計{len(trade_log)}件')

    tracking.save_trade_log(trade_log, path=BACKTEST_TRADE_LOG_PATH)
    stats = tracking.compute_performance_stats(trade_log)

    summary_path = os.path.join(OUT_DIR, f'backtest_summary_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json')
    with open(summary_path, 'w', encoding='utf-8') as f:
        json.dump({
            'universe_size': len(tickers_data),
            'date_range': [all_dates[0].strftime('%Y-%m-%d'), all_dates[-1].strftime('%Y-%m-%d')],
            'trade_log_count': len(trade_log),
            'performance_stats': stats,
        }, f, ensure_ascii=False, indent=2)

    print('=' * 60)
    print(f'[backtest] 完了。結果: {summary_path}')
    print(f'[backtest] LONG+SHORT合算: {stats["long_short"]}')
    print(f'[backtest] LONGのみ: {stats["long_only"]}')
    print(f'[backtest] SHORTのみ: {stats["short_only"]}')
    print(f'[backtest] 保有中(未決済): {stats["open_positions"]}件')


if __name__ == '__main__':
    run_backtest()
