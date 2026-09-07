#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
universe.py — 東証全銘柄（プライム・スタンダード・グロースの内国株式）のティッカー一覧取得（Phase 3）

日本取引所グループ（JPX）が毎月更新して公開している「東証上場銘柄一覧」
（https://www.jpx.co.jp/markets/statistics-equities/misc/01.html 内の data_j.xls(x)）
を実行時にダウンロードし、市場区分でフィルタしてyfinance用ティッカー（例: 7203.T）の
一覧を作る。

【2026-09-07：実データで確認・修正】
実際にJPXの一覧ページを取得したところ、配布ファイルが `data_j.xls`（旧形式）から
`data_j.xlsx`（新形式）に変わっていたことが判明した。以前の正規表現
（`data_j\.xls"` で終端を要求）は `data_j.xlsx"` にマッチせず、一覧ページからの
動的取得が常に失敗してハードコードのフォールバックURL（これも`.xls`のまま＝404）に
落ちる状態になっていた。拡張子を `.xlsx?` に緩めて対応済み（`pd.read_excel`は
拡張子ではなくファイル内容から自動でopenpyxl/xlrdを使い分けるため、URLの拡張子が
どちらでも読み込み自体は問題ない）。

【運用上の注意】
- data_j.xls(x) のURL（ファイル名部分のハッシュ）はJPXが月次更新のたびに変更するため、
  一覧ページのHTMLから最新のリンクを都度取得する。何らかの理由でHTML構造が変わり
  リンクが取得できない場合は、フォールバックとして本ファイル修正時点のURLを使う
  （こちらも数ヶ月で無効化される可能性がある。またJPXが将来再び拡張子を変更した
  場合は、この正規表現・フォールバックURLの見直しが必要）。
- 対象は「プライム（内国株式）」「スタンダード（内国株式）」「グロース（内国株式）」の
  3区分のみ。ETF/ETN、REIT、PRO Market、外国株式、出資証券は対象外。
- 約3,900銘柄を毎回スクレイピングするため、JPX側に過度な負荷をかけないよう
  1回のGitHub Actions実行につき1回だけ取得しキャッシュする設計にしている。
"""

import re
import sys

import pandas as pd
import requests

JPX_LIST_PAGE = 'https://www.jpx.co.jp/markets/statistics-equities/misc/01.html'
# 2026-09-07に実データで確認したURL（拡張子が.xlsxに変更されていた）。
# 一覧ページからの動的取得が失敗した場合のみ使用。
JPX_FALLBACK_XLS = 'https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xlsx'

TARGET_MARKET_SEGMENTS = [
    'プライム（内国株式）',
    'スタンダード（内国株式）',
    'グロース（内国株式）',
]

# JPXからの取得が完全に失敗した場合に最低限の分析を継続するためのフォールバック銘柄
# （引き継ぎ資料に記載されていた当初の5銘柄）。
FALLBACK_TICKERS = ['6758.T', '7203.T', '9984.T', '6861.T', '8306.T']


def _resolve_xls_url(timeout=20):
    """一覧ページのHTMLから最新のdata_j.xls(x)リンクを取得する。失敗時はフォールバックURL。"""
    try:
        resp = requests.get(JPX_LIST_PAGE, timeout=timeout,
                             headers={'User-Agent': 'Mozilla/5.0 (KurosukeBot)'})
        resp.raise_for_status()
        # 【2026-09-07修正】JPXが配布形式を.xls→.xlsxに変更したため、両方にマッチするようにする
        m = re.search(r'href="([^"]+data_j\.xlsx?)"', resp.text)
        if m:
            url = m.group(1)
            if url.startswith('/'):
                url = 'https://www.jpx.co.jp' + url
            return url
    except Exception as e:
        print(f'[universe] 一覧ページからのURL取得に失敗（フォールバックURLを使用）: {e}', file=sys.stderr)
    return JPX_FALLBACK_XLS


def fetch_jpx_listed_df(timeout=60):
    """
    JPXのdata_j.xls(x)を取得してDataFrameで返す。
    列: 'コード', '銘柄名', '市場・商品区分', ... （JPX公開ファイルの原本の列構成のまま）
    取得・パース失敗時は例外を送出する（呼び出し側でフォールバック処理をすること）。
    """
    xls_url = _resolve_xls_url()
    print(f'[universe] data_j.xls(x) 取得元: {xls_url}')

    resp = requests.get(xls_url, timeout=timeout, headers={'User-Agent': 'Mozilla/5.0 (KurosukeBot)'})
    resp.raise_for_status()

    from io import BytesIO
    # pd.read_excel はURLの拡張子ではなくファイル内容（マジックバイト）から
    # 自動でエンジンを選ぶため、.xls(xlrd)でも.xlsx(openpyxl)でも明示指定は不要
    # （両パッケージともrequirements.txtに含まれている）。
    df = pd.read_excel(BytesIO(resp.content))
    return df


def get_all_tse_tickers(market_segments=None, exclude_codes=None):
    """
    プライム・スタンダード・グロースの内国株式ティッカー一覧（'XXXX.T'形式）を返す。

    戻り値: (tickers: list[str], meta_df: pandas.DataFrame or None)
      meta_df は 'code', 'name', 'market', 'sector_code', 'sector_name' 列を持つ
      絞り込み後のDataFrame（銘柄名表示・セクターレンズ用）。33業種コード列が
      見つからない場合、sector_code/sector_nameはNoneで埋められる（呼び出し側は
      「データ不足」として扱うこと。列名の想定違いは初回実行時のログで確認する）。
      JPXからの取得に失敗した場合は (FALLBACK_TICKERS, None) を返し、標準エラー出力に警告を出す。
    """
    segments = market_segments or TARGET_MARKET_SEGMENTS
    try:
        raw = fetch_jpx_listed_df()
    except Exception as e:
        print(f'[universe] JPX銘柄一覧の取得に失敗しました。フォールバック銘柄で継続します: {e}',
              file=sys.stderr)
        return list(FALLBACK_TICKERS), None

    code_col = None
    market_col = None
    name_col = None
    sector_code_col = None
    sector_name_col = None
    for col in raw.columns:
        col_str = str(col)
        if code_col is None and 'コード' in col_str and '業種' not in col_str:
            code_col = col
        if market_col is None and '市場' in col_str and '区分' in col_str:
            market_col = col
        if name_col is None and '銘柄名' in col_str:
            name_col = col
        # 17業種コード/区分と区別するため「33」を含む列のみを対象にする
        if sector_code_col is None and '33業種' in col_str and 'コード' in col_str:
            sector_code_col = col
        if sector_name_col is None and '33業種' in col_str and '区分' in col_str:
            sector_name_col = col

    if code_col is None or market_col is None:
        print(f'[universe] 想定した列（コード／市場・商品区分）が見つかりません。列一覧: {list(raw.columns)}',
              file=sys.stderr)
        return list(FALLBACK_TICKERS), None

    if sector_code_col is None or sector_name_col is None:
        print(f'[universe] 33業種コード／33業種区分の列が見つかりません（セクターレンズは'
              f'データ不足扱いになります）。列一覧: {list(raw.columns)}', file=sys.stderr)

    df = raw[raw[market_col].isin(segments)].copy()
    if df.empty:
        print(f'[universe] 市場区分フィルタ後の該当銘柄が0件でした（区分値の想定違いの可能性）。'
              f'実際の値: {raw[market_col].unique().tolist()[:20]}', file=sys.stderr)
        return list(FALLBACK_TICKERS), None

    df['code'] = df[code_col].astype(str).str.strip()
    if exclude_codes:
        df = df[~df['code'].isin(exclude_codes)]

    df['ticker'] = df['code'] + '.T'
    if name_col is not None:
        df = df.rename(columns={name_col: 'name'})
    else:
        df['name'] = None
    df = df.rename(columns={market_col: 'market'})

    if sector_code_col is not None:
        df['sector_code'] = df[sector_code_col].astype(str).str.strip()
    else:
        df['sector_code'] = None
    if sector_name_col is not None:
        df['sector_name'] = df[sector_name_col]
    else:
        df['sector_name'] = None

    tickers = df['ticker'].tolist()
    print(f'[universe] JPX銘柄一覧を取得: 対象{len(tickers)}銘柄 '
          f'(区分内訳: {df["market"].value_counts().to_dict()})')

    return tickers, df[['code', 'name', 'market', 'sector_code', 'sector_name', 'ticker']].reset_index(drop=True)
