#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
refresh_jquants_cache.py — data/jquants_cache/ の鮮度を保つための定期バッチ。

【背景・課題】
backfill_jquants.py（Phase1で作成）の backfill_one() は「既にbars.json/fins.jsonが
存在すればスキップ」するだけで、(1) 新規上場銘柄への追従、(2) 既存銘柄の決算更新・
株価取得ウィンドウのスライドへの追従、のどちらの仕組みも持っていなかった。
Phase2でこのキャッシュを本番のcollector.py（fundamental_snapshot）・
compute_sector_stats.py（業種平均）が読むようになったため、鮮度を保つ運用が必要になった。

【レート制限との兼ね合い・設計方針】
J-Quants Freeプランは5回/分（jquants_client.pyは安全マージンを見て13秒間隔＝約4.6回/分）。
全銘柄（約3,900銘柄×2エンドポイント=約7,800リクエスト）を1回で更新すると理論値で
約26時間かかり、1ジョブ（GitHub Actionsは最大6時間）では終わらない。
そのため「1回の実行では時間予算内で処理できる分だけ進める」設計にし、GitHub Actionsで
日次実行することで数週間かけて全体を一巡させる。優先順位は
  1. 新規上場銘柄（キャッシュに無いもの）
  2. 既存銘柄のうち、最終更新（fins.jsonのmtime）が最も古いもの
の順。処理しきれなかった分は次回実行時に続きから優先的に処理される。

【2026-09-07追記：GitHub Actions無料枠への配慮】
collect_data.yml自体が設計上230分×週6日≒月5,900分規模でGitHub Actions無料枠
（非公開リポジトリ月2,000分）を単独で超えうるため、本バッチの既定値は小さめ
（1回30分・週2回想定）にしてある。全銘柄を一巡させるのに数ヶ月かかる計算になるが、
新規上場銘柄は毎回最優先されるため実害は小さいと判断した。実際に使える予算は
GitHubのBilling画面で確認し、必要に応じて環境変数で調整すること。

使い方:
    python refresh_jquants_cache.py
環境変数:
    JQUANTS_REFRESH_BUDGET_MINUTES（既定30）… 1回の実行での処理時間の目安
    JQUANTS_REFRESH_STALE_AFTER_DAYS（既定30）… この日数を超えて更新されていない
        銘柄だけを「更新対象」とみなす（鮮度内の銘柄まで毎回舐めて時間を浪費しないため）
"""

import os
import sys
import time

from backfill_jquants import backfill_one, resolve_universe, resolve_free_plan_window, CACHE_DIR

BUDGET_MINUTES = float(os.getenv('JQUANTS_REFRESH_BUDGET_MINUTES', '30'))
STALE_AFTER_DAYS = float(os.getenv('JQUANTS_REFRESH_STALE_AFTER_DAYS', '30'))


def _cache_age_days(ticker4):
    """未キャッシュならNone（＝新規銘柄扱い）。キャッシュ済みならfins.jsonの経過日数。"""
    fins_path = os.path.join(CACHE_DIR, ticker4, 'fins.json')
    if not os.path.exists(fins_path):
        return None
    return (time.time() - os.path.getmtime(fins_path)) / 86400


def build_refresh_plan(universe):
    """新規銘柄を最優先、その後は鮮度切れの既存銘柄を古い順に並べた処理リストを作る。"""
    new_tickers = []
    stale_tickers = []  # (age_days, universe_entry)
    for u in universe:
        age = _cache_age_days(u['ticker4'])
        if age is None:
            new_tickers.append(u)
        elif age >= STALE_AFTER_DAYS:
            stale_tickers.append((age, u))

    stale_tickers.sort(key=lambda t: t[0], reverse=True)  # 経過日数が大きい（＝古い）順
    ordered = new_tickers + [u for _, u in stale_tickers]
    return ordered, len(new_tickers), len(stale_tickers)


def main():
    date_from, date_to = resolve_free_plan_window()
    universe = resolve_universe()
    if not universe:
        print('[refresh] 対象銘柄が0件のため終了します')
        return 1

    plan, new_count, stale_count = build_refresh_plan(universe)
    print(f'[refresh] 対象銘柄{len(universe)}件中、新規{new_count}件・鮮度切れ'
          f'（{STALE_AFTER_DAYS}日超）{stale_count}件を優先度順に処理します'
          f'（時間予算{BUDGET_MINUTES}分）')

    if not plan:
        print('[refresh] 更新対象が無いため終了します（全銘柄が鮮度内）')
        return 0

    start = time.time()
    budget_seconds = BUDGET_MINUTES * 60
    processed, ok, ng = 0, 0, 0
    for u in plan:
        if time.time() - start >= budget_seconds:
            print(f'[refresh] 時間予算（{BUDGET_MINUTES}分）に達したため打ち切ります'
                  f'（処理済み{processed}/{len(plan)}件、残りは次回実行で優先的に処理されます）')
            break
        ticker4 = u['ticker4']
        print(f'[refresh] ({processed + 1}/{len(plan)}) {ticker4} ({u.get("name") or ""}) 更新中...')
        if backfill_one(ticker4, date_from, date_to, force=True):
            ok += 1
        else:
            ng += 1
        processed += 1

    print(f'[refresh] 完了: 成功{ok}件 / 失敗{ng}件 / 未処理{len(plan) - processed}件')
    return 0


if __name__ == '__main__':
    sys.exit(main())
