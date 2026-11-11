#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
swing_test_run.py — スイング3型の「試し投稿」（2026-11-11追加）。

毎晩の収集を待たずに、その場で Yahoo Finance から日足を取り → swing_live.run で判定 → swing_poster で投稿する。
仮想売買の記録（data/swing_trade_log.json）・判定結果（data/swing_scan.json）・投稿済みマーカーは一切変えない
（一時フォルダのコピーで動かす）。投稿のタイトルには【テスト投稿】と付く。

  python swing_test_run.py                 # 全銘柄（本番と同じ対象。株価取得に20〜40分）
  python swing_test_run.py --max-tickers 300
  python swing_test_run.py --dry-run       # 投稿せず内容を表示
  python swing_test_run.py --as-of 2026-11-10 --ignore-regime   # 前日の引けで判定・地合いフィルターを外して候補を出す
"""

import argparse
import os
import shutil
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix='swing_test_')
for src, name in (('data/swing_trade_log.json', 'swing_trade_log.json'),):
    if os.path.exists(src):
        shutil.copy(src, os.path.join(TMP, name))
os.environ['SWING_LOG_PATH'] = os.path.join(TMP, 'swing_trade_log.json')
os.environ['SWING_SCAN_PATH'] = os.path.join(TMP, 'swing_scan.json')
os.environ['SWING_POSTED_MARKER'] = os.path.join(TMP, 'posted.txt')
os.environ['SWING_POST_TITLE_PREFIX'] = '【テスト投稿】'

import pandas as pd  # noqa: E402

import collector  # noqa: E402
import swing_live  # noqa: E402
import swing_poster  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--max-tickers', type=int, default=0)
    ap.add_argument('--window-minutes', type=float, default=15)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--as-of', default='', help='この日（YYYY-MM-DD）の引けまでのデータで判定する')
    ap.add_argument('--ignore-regime', action='store_true', help='地合いフィルターを外して候補を出す（テスト表示用）')
    args = ap.parse_args()
    if args.ignore_regime:
        swing_live.LIVE_RULES = {k: (h, st, False) for k, (h, st, _) in swing_live.LIVE_RULES.items()}
        swing_poster.TITLE_PREFIX = '【テスト投稿・地合いフィルターなし】'

    tickers, meta_df, _, _ = collector.resolve_universe()
    if args.max_tickers:
        tickers = tickers[:args.max_tickers]
    names = {}
    if meta_df is not None:
        names = {r['ticker']: str(r['name']).strip() for r in meta_df.to_dict('records') if pd.notna(r.get('name'))}
    print(f'[swing-test] {len(tickers)}銘柄の日足を取得します（一時フォルダ {TMP}）', flush=True)
    histories, failed = collector.fetch_price_histories(tickers, window_minutes=args.window_minutes)
    print(f'[swing-test] 取得 成功{len(histories)}／失敗{len(failed)}', flush=True)
    if args.as_of:
        cut = pd.Timestamp(args.as_of)
        histories = {t: df.loc[:cut] for t, df in histories.items() if len(df.loc[:cut])}
        print(f'[swing-test] {args.as_of} の引けまでのデータで判定します', flush=True)
    out = swing_live.run(histories, names=names)
    print(f"[swing-test] {out['date']} 地合い{'良好' if out['regime']['ok'] else '悪化'}／"
          + ' ／ '.join(f"{s['label']}：該当{s['total_hits']}件" for s in out['signals'].values()), flush=True)
    sys.argv = [sys.argv[0]] + (['--dry-run'] if args.dry_run else ['--force'])
    return swing_poster.main()


if __name__ == '__main__':
    sys.exit(main())
