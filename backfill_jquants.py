#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backfill_jquants.py — J-Quants APIから株価四本値・財務情報を取得し、ローカルにキャッシュする
（バックテスト用データ準備。Phase 1は小規模ユニバースでの動作確認が目的）

【使い方（Phase 1・小規模テスト）】
  # 環境変数 JQUANTS_TICKERS にカンマ区切りの銘柄コード（4桁）を指定して実行
  # 例：トヨタ・ソニーG・任天堂など数十銘柄
  JQUANTS_TICKERS="7203,6758,7974,9984,8306" python backfill_jquants.py

【使い方（Phase 2・全銘柄）】
  # JQUANTS_TICKERS を指定しなければ、上場銘柄一覧（/equities/master）から
  # プライム・スタンダード・グロースの内国株式を全件対象にする
  python backfill_jquants.py

【Freeプランのデータ取得可能期間について】
株価四本値・財務情報とも「直近12週間前〜2年12週間前」の固定窓のみアクセス可（2026-09-04
確認）。本スクリプトは今日の日付からこの窓を自動計算する（下記 resolve_free_plan_window）。
正確な境界（12週間の定義・タイムゾーン等）はJ-Quants側の実装次第で数日ずれる可能性がある
ため、境界ぎりぎりの日付でAPIがエラーを返す場合はJQUANTS_WINDOW_MARGIN_DAYSで数日分の
余裕を追加できるようにしている。

【銘柄コードの形式について】
J-Quants APIは5桁コード（例：トヨタ=72030）を返すことが多い。本スクリプトが受け取る
JQUANTS_TICKERSは4桁（例：7203）を想定し、API呼び出し時にそのまま渡す（J-Quants側で
4桁指定でも解決されることを期待）。レスポンスの'Code'フィールドが5桁で返ってきた場合、
末尾の0を取り除いて4桁化しキャッシュのキーに使う（jquants_code_to_ticker4参照）。
これはPhase 1で実データを見ながら要検証。
"""

import json
import os
import sys
from datetime import datetime, timedelta

from jquants_client import get_daily_quotes, get_fin_summary, get_listed_master
from universe import get_all_tse_tickers

CACHE_DIR = os.getenv('JQUANTS_CACHE_DIR') or 'data/jquants_cache'
TARGET_MARKET_NAMES = ['プライム', 'スタンダード', 'グロース']
WINDOW_MARGIN_DAYS = int(os.getenv('JQUANTS_WINDOW_MARGIN_DAYS', '3'))


def jquants_code_to_ticker4(code):
    """'86970'のような5桁コードを'8697'のような4桁コードに変換する（末尾0を除去）。
    既に4桁ならそのまま返す。"""
    code = str(code)
    if len(code) == 5 and code.endswith('0'):
        return code[:4]
    return code


def resolve_free_plan_window(today=None, margin_days=WINDOW_MARGIN_DAYS):
    """
    Freeプランの固定窓（直近12週間前〜2年12週間前）を計算する。
    marginを引く・足すことで境界エラーを避ける安全マージンを設ける。
    戻り値: (date_from: 'YYYY-MM-DD', date_to: 'YYYY-MM-DD')
    """
    today = today or datetime.now().date()
    # to（新しい側の境界）：直近12週間前より、安全マージン分だけさらに古い日付にする
    to_date = today - timedelta(weeks=12) - timedelta(days=margin_days)
    # from（古い側の境界）：2年12週間前より、安全マージン分だけ新しい日付にする
    # （＝両端とも「窓の内側」に安全マージンを取ることで境界エラーを避ける）
    from_date = today - timedelta(weeks=12) - timedelta(days=2 * 365) + timedelta(days=margin_days)
    return from_date.isoformat(), to_date.isoformat()


def resolve_sector_by_code4():
    """
    【2026-09-07追加】JPX一覧（universe.py・33業種コード対応済み）からticker4 ->
    (sector_code, sector_name) の辞書を作る。バックテストの業種別集計
    （backtest.py参照）で使うための下ごしらえ。JPX取得に失敗しても
    backfill自体は継続できるよう、失敗時は空辞書を返す（sector_code=Noneのまま進む）。
    """
    try:
        _tickers, meta_df = get_all_tse_tickers()
    except Exception as e:
        print(f'[backfill] JPX業種コード取得に失敗（業種別集計は無しで継続）: {e}', file=sys.stderr)
        return {}
    if meta_df is None:
        return {}
    out = {}
    for row in meta_df.to_dict('records'):
        code = str(row.get('code') or '').strip()
        sector_code = row.get('sector_code')
        if not code or sector_code is None or str(sector_code) == 'None':
            continue
        out[code] = (str(sector_code).strip(), row.get('sector_name'))
    return out


def resolve_universe():
    """
    JQUANTS_TICKERS環境変数があればそれを使う（Phase 1用）。無ければ /equities/master から
    プライム・スタンダード・グロースの内国株式一覧を取得する（Phase 2用）。
    戻り値: list[dict]（{'ticker4': '7203', 'name': 'トヨタ自動車',
                          'sector_code': '3050', 'sector_name': '輸送用機器'} 等。
             業種コードが取得できなかった銘柄はsector_code/sector_name=None）
    """
    override = os.getenv('JQUANTS_TICKERS')
    if override:
        tickers = [t.strip() for t in override.split(',') if t.strip()]
        print(f'[backfill] JQUANTS_TICKERS指定により{len(tickers)}銘柄に絞り込みます（Phase 1想定）')
        return [{'ticker4': t, 'name': None, 'sector_code': None, 'sector_name': None} for t in tickers]

    print('[backfill] 上場銘柄一覧を取得しています（/equities/master）...')
    master = get_listed_master()
    sector_by_code4 = resolve_sector_by_code4()
    matched = 0
    out = []
    for row in master:
        mkt_nm = row.get('MktNm') or ''
        if mkt_nm not in TARGET_MARKET_NAMES:
            continue
        ticker4 = jquants_code_to_ticker4(row.get('Code'))
        sector_code, sector_name = sector_by_code4.get(ticker4, (None, None))
        if sector_code is not None:
            matched += 1
        out.append({'ticker4': ticker4, 'name': row.get('CoName'),
                     'sector_code': sector_code, 'sector_name': sector_name})
    print(f'[backfill] 対象{len(out)}銘柄（プライム/スタンダード/グロース、内国株式以外も含む可能性あり'
          f'＝Phase 2で要精査）。業種コード突合: {matched}/{len(out)}件')
    return out


def backfill_one(ticker4, date_from, date_to, force=False):
    """1銘柄分の株価四本値・財務情報を取得し、キャッシュファイルに保存する。"""
    ticker_dir = os.path.join(CACHE_DIR, ticker4)
    bars_path = os.path.join(ticker_dir, 'bars.json')
    fins_path = os.path.join(ticker_dir, 'fins.json')

    if not force and os.path.exists(bars_path) and os.path.exists(fins_path):
        print(f'[backfill] {ticker4}: キャッシュ済みのためスキップ（再取得するには force=True）')
        return True

    os.makedirs(ticker_dir, exist_ok=True)
    try:
        bars = get_daily_quotes(ticker4, date_from=date_from, date_to=date_to)
        fins = get_fin_summary(ticker4)
    except Exception as e:
        print(f'[backfill] {ticker4}: 取得失敗: {e}', file=sys.stderr)
        return False

    with open(bars_path, 'w', encoding='utf-8') as f:
        json.dump(bars, f, ensure_ascii=False)
    with open(fins_path, 'w', encoding='utf-8') as f:
        json.dump(fins, f, ensure_ascii=False)

    print(f'[backfill] {ticker4}: 株価{len(bars)}件・財務開示{len(fins)}件を保存しました')
    return True


def main():
    date_from, date_to = resolve_free_plan_window()
    print(f'[backfill] 対象期間（Freeプラン固定窓）: {date_from} 〜 {date_to}')

    universe = resolve_universe()
    if not universe:
        print('[backfill] 対象銘柄が0件のため終了します')
        return

    os.makedirs(CACHE_DIR, exist_ok=True)
    meta_path = os.path.join(CACHE_DIR, '_universe.json')
    with open(meta_path, 'w', encoding='utf-8') as f:
        json.dump({'date_from': date_from, 'date_to': date_to, 'universe': universe,
                    'generated_at': datetime.now().isoformat()}, f, ensure_ascii=False, indent=2)

    ok, ng = 0, 0
    for i, u in enumerate(universe):
        ticker4 = u['ticker4']
        print(f'[backfill] ({i + 1}/{len(universe)}) {ticker4} ({u.get("name") or ""}) 取得中...')
        if backfill_one(ticker4, date_from, date_to):
            ok += 1
        else:
            ng += 1

    print(f'[backfill] 完了: 成功{ok}件 / 失敗{ng}件。キャッシュ先: {CACHE_DIR}')


if __name__ == '__main__':
    main()
