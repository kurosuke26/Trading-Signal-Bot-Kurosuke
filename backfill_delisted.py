#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backfill_delisted.py — バックテスト期間中に上場廃止になった銘柄の株価・財務をJ-Quantsから取得し、
data/jquants_cache に追加する（生存者バイアス対策）。

【背景】
backfill_jquants.py は「実行時点で上場している銘柄」（/equities/master の最新一覧）だけを取得するため、
期間中に上場廃止（TOB・MBO・経営統合・破綻など）になった銘柄がバックテストから抜け落ちていた。
成績の悪かった銘柄ほど消えやすいため、検証結果が実力より良く出る方向に偏る。

【やること】
1. Freeプランの取得可能期間の中で、約3か月おきの日付の上場銘柄一覧（/equities/master?date=）を取得
2. どれかの時点でプライム・スタンダード・グロースに上場していたが、_universe.json（現在の一覧）に
   無い銘柄を「期間中の上場廃止銘柄」とみなす
3. その銘柄の株価四本値・財務情報を backfill_jquants.backfill_one() で取得（キャッシュ済みはスキップ）
4. data/jquants_cache/_delisted.json に一覧（最後に一覧で確認できた日付つき）を保存

evaluate_sakata.py / evaluate_strategy.py は _universe.json と _delisted.json の両方を読む。
既存の backtest.py などは従来どおり _universe.json だけを読む（挙動は変わらない）。

【実行方法】
  python backfill_delisted.py            # 一覧の作成＋全銘柄の取得（1銘柄あたり約26秒）
  python backfill_delisted.py --list-only  # 一覧の作成だけ
"""

import argparse
import json
import os
import sys
from datetime import date, timedelta

from backfill_jquants import (CACHE_DIR, TARGET_MARKET_NAMES, backfill_one, jquants_code_to_ticker4,
                              resolve_free_plan_window)
from jquants_client import get_listed_master

DELISTED_PATH = os.path.join(CACHE_DIR, '_delisted.json')
SNAPSHOT_STEP_DAYS = 91


def snapshot_dates(date_from, date_to):
    d = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    out = []
    while d <= end:
        out.append(d.isoformat())
        d += timedelta(days=SNAPSHOT_STEP_DAYS)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--list-only', action='store_true')
    args = ap.parse_args()

    with open(os.path.join(CACHE_DIR, '_universe.json'), encoding='utf-8') as f:
        meta = json.load(f)
    current = {u['ticker4'] for u in meta['universe']}
    # Freeプランの取得可能期間（実行日から自動計算）。この窓の外の日付を指定するとAPIが400を返す
    date_from, date_to = resolve_free_plan_window()

    seen = {}  # ticker4 -> {'name','market','first_seen','last_seen'}
    for d in snapshot_dates(date_from, date_to):
        try:
            rows = get_listed_master(date=d)
        except Exception as e:
            print(f'[delisted] {d} の上場銘柄一覧の取得に失敗: {e}', file=sys.stderr)
            continue
        n_target = 0
        for row in rows:
            if (row.get('MktNm') or '') not in TARGET_MARKET_NAMES:
                continue
            n_target += 1
            t4 = jquants_code_to_ticker4(row.get('Code'))
            rec = seen.setdefault(t4, {'ticker4': t4, 'name': row.get('CoName'), 'market': row.get('MktNm'),
                                       'first_seen': d, 'last_seen': d})
            rec['last_seen'] = d
        print(f'[delisted] {d}: 対象市場 {n_target} 銘柄', flush=True)

    delisted = sorted((r for t4, r in seen.items() if t4 not in current), key=lambda r: r['ticker4'])
    with open(DELISTED_PATH, 'w', encoding='utf-8') as f:
        json.dump({'date_from': date_from, 'date_to': date_to, 'delisted': delisted}, f,
                  ensure_ascii=False, indent=2)
    print(f'[delisted] 期間中に上場廃止（現在の一覧に無い）銘柄: {len(delisted)}件 → {DELISTED_PATH}', flush=True)
    if args.list_only:
        return

    ok = ng = 0
    for k, r in enumerate(delisted):
        print(f'[delisted] ({k + 1}/{len(delisted)}) {r["ticker4"]} {r["name"]}（最終確認 {r["last_seen"]}）', flush=True)
        if backfill_one(r['ticker4'], date_from, date_to):
            ok += 1
        else:
            ng += 1
    print(f'[delisted] 完了: 成功{ok}件 / 失敗{ng}件', flush=True)


if __name__ == '__main__':
    main()
