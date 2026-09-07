#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
jquants_client.py — J-Quants API V2 の薄いクライアント（バックテスト用データ取得）

【重要な前提（2026-09-04時点でJPX公式サイトを調査して確認）】
- J-Quants APIは2026-06-01にV1が終了し、現在はV2のみ（ベースURL: https://api.jquants.com/v2）。
- 認証はV1の「メールアドレス/パスワード→リフレッシュトークン→IDトークン」方式ではなく、
  V2では「APIキー方式」に変更されている。J-Quantsのダッシュボード（設定 » API キー）で
  発行したAPIキーを、リクエストヘッダー `x-api-key` に指定するだけでよい。
- レスポンスは統一形式で `{"data": [...], "pagination_key": "..."}` のような形になり、
  カラム名も短縮形（Open→O, Close→C, Volume→Vo 等）に変わっている。

【本ファイルが対象とする3エンドポイント】
- 株価四本値: GET /v2/equities/bars/daily （params: code, from, to）
- 財務情報:   GET /v2/fins/summary        （params: code）
- 上場銘柄一覧: GET /v2/equities/master    （params: date）

【契約プランについて】
ユーザーはFreeプラン契約（2026-09-04時点で確認済み）。Freeプランは株価四本値・財務情報とも
「直近12週間前〜2年12週間前」の固定窓のみアクセス可（つまり直近3ヶ月弱のデータと、
2年3ヶ月より前のデータは取得できない）。詳細は`Claude outputs/2026-09-04-jquants-backtest-design.md`
を参照。

【正直な注意点：フィールド名の確度について】
本ファイルのフィールド名（O/H/L/C/Vo、EPS/BPS/DivAnn等）は、JPX公式サイト
（jpx-jquants.com）の複数のリファレンスページを2026-09-04にWeb調査して確認したものだが、
このセッションの開発環境からは実際にJ-Quants APIを叩いて動作確認ができていない
（ネットワーク制約・APIキー未保有のため）。Phase 1の小規模テストで実際のレスポンスと
突き合わせ、フィールド名が違っていれば本ファイルを修正すること
（`_debug_dump_sample()` で生レスポンスを確認できるようにしてある）。

【レート制限】
FreeプランはAPI呼び出し5回/分という記載を確認済み。本クライアントは1リクエストごとに
最低 MIN_INTERVAL_SECONDS（既定13秒）空けるペーシングと、429（レート制限超過）時の
指数バックオフ・リトライを組み込んでいる。
"""

import json
import os
import sys
import time
from datetime import datetime

import requests

try:
    # 【2026-09-04追加】このリポジトリは元々python-dotenvを使っておらず、.envはGitHub
    # Actions側のsecrets設定用の参考ファイルという位置づけだった（コード内でload_dotenv()
    # している箇所が無かった）。ローカル実行では.envに書いた値がそのままでは読み込まれない
    # ため、ここでdotenvを使って.envを読み込む（requirements.txtにpython-dotenvを追加済み）。
    # パッケージ未インストールでも動くよう、失敗時は「環境変数を手動で設定してください」で
    # フォールバックする。
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    print('[jquants_client] python-dotenv未インストールのため.envは自動読み込みされません。'
          '`pip install -r requirements.txt` を実行するか、PowerShellで '
          '$env:JQUANTS_API_KEY="..." のように環境変数を直接設定してください。', file=sys.stderr)

BASE_URL = 'https://api.jquants.com/v2'
API_KEY_ENV = 'JQUANTS_API_KEY'

# Freeプラン：5回/分という記載を確認済み。安全マージンを見て1リクエストあたり最低13秒空ける
# （5回/分 = 12秒間隔が理論値だが、少し余裕を持たせる）。
MIN_INTERVAL_SECONDS = float(os.getenv('JQUANTS_MIN_INTERVAL_SECONDS', '13'))
MAX_RETRIES = 5

_last_request_at = 0.0


def _api_key():
    key = os.getenv(API_KEY_ENV)
    if not key:
        raise RuntimeError(
            f'環境変数 {API_KEY_ENV} が設定されていません。.env に '
            f'{API_KEY_ENV}=あなたのAPIキー を追加してください（J-Quantsダッシュボードの'
            '「設定 » API キー」から発行できます）。'
        )
    return key


def _pace():
    """MIN_INTERVAL_SECONDSを守るための待機。"""
    global _last_request_at
    elapsed = time.time() - _last_request_at
    if elapsed < MIN_INTERVAL_SECONDS:
        time.sleep(MIN_INTERVAL_SECONDS - elapsed)
    _last_request_at = time.time()


def _get(path, params=None, max_retries=MAX_RETRIES):
    """
    GETリクエストの共通処理。ページネーション（pagination_key）を自動で追い、
    全ページ分の 'data' 配列を結合して返す。429時は指数バックオフでリトライする。
    """
    url = f'{BASE_URL}{path}'
    headers = {'x-api-key': _api_key()}
    params = dict(params or {})

    all_data = []
    page = 0
    while True:
        page += 1
        attempt = 0
        while True:
            attempt += 1
            _pace()
            try:
                resp = requests.get(url, headers=headers, params=params, timeout=30)
            except requests.exceptions.RequestException as e:
                if attempt >= max_retries:
                    raise
                wait = min(60, 5 * (2 ** (attempt - 1)))
                print(f'[jquants_client] {path} 通信エラー（試行{attempt}）: {e} '
                      f'→ {wait}秒待って再試行', file=sys.stderr)
                time.sleep(wait)
                continue

            if resp.status_code == 429:
                if attempt >= max_retries:
                    resp.raise_for_status()
                retry_after = resp.headers.get('Retry-After')
                wait = float(retry_after) if retry_after else min(90, 10 * (2 ** (attempt - 1)))
                print(f'[jquants_client] {path} レート制限超過（試行{attempt}） → {wait}秒待って再試行',
                      file=sys.stderr)
                time.sleep(wait)
                continue

            if resp.status_code >= 500:
                if attempt >= max_retries:
                    resp.raise_for_status()
                wait = min(60, 5 * (2 ** (attempt - 1)))
                print(f'[jquants_client] {path} サーバーエラー{resp.status_code}（試行{attempt}） '
                      f'→ {wait}秒待って再試行', file=sys.stderr)
                time.sleep(wait)
                continue

            resp.raise_for_status()
            break

        body = resp.json()
        chunk = body.get('data', [])
        all_data.extend(chunk)

        pagination_key = body.get('pagination_key')
        if not pagination_key:
            break
        params['pagination_key'] = pagination_key
        print(f'[jquants_client] {path} ページ{page}取得（累計{len(all_data)}件）ページネーション継続',
              file=sys.stderr)

    return all_data


def get_daily_quotes(code, date_from=None, date_to=None):
    """
    株価四本値（日足）を取得する。
    戻り値: list[dict]（各要素は 'Date','Code','O','H','L','C','Vo','Va','AdjC','AdjVo' 等を持つ）
    """
    params = {'code': code}
    if date_from:
        params['from'] = date_from
    if date_to:
        params['to'] = date_to
    return _get('/equities/bars/daily', params)


def get_fin_summary(code):
    """
    財務情報（開示ベース）を取得する。
    戻り値: list[dict]（'DiscDate','DocType','EPS','BPS','DivAnn','FDivAnn' 等を持つ）
    """
    return _get('/fins/summary', {'code': code})


def get_listed_master(date=None):
    """
    上場銘柄一覧を取得する。dateを省略すると直近時点の一覧が返る想定。
    戻り値: list[dict]（'Code','CoName','MktNm'（プライム/スタンダード/グロース）等を持つ）
    """
    params = {}
    if date:
        params['date'] = date
    return _get('/equities/master', params)


def _debug_dump_sample(code='72030', out_path='data/jquants_cache/_debug_sample.json'):
    """
    Phase 1の動作確認用：1銘柄分の生レスポンスをファイルに保存する。
    フィールド名が本ファイルの想定と違っていないか、この内容を見て確認すること。
    """
    sample = {
        'daily_quotes_sample': get_daily_quotes(code)[:3],
        'fin_summary_sample': get_fin_summary(code)[:3],
        'listed_master_sample': get_listed_master()[:3],
        'fetched_at': datetime.now().isoformat(),
    }
    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(sample, f, ensure_ascii=False, indent=2)
    print(f'[jquants_client] サンプルレスポンスを保存しました: {out_path}')
    return sample


if __name__ == '__main__':
    # 動作確認用：python jquants_client.py [銘柄コード] で1銘柄分のサンプルを取得・表示する
    code_arg = sys.argv[1] if len(sys.argv) > 1 else '72030'
    _debug_dump_sample(code_arg)
