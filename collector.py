#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
collector.py — データ収集・判定バッチ（2:00〜6:00 JST頃に実行する想定）

【今回の変更の背景】
以前は「データ取得」と「Discord投稿」を1本のスクリプト（毎朝7:00起動）で
まとめて行っていたが、全銘柄（約3,900）を対象にすると投稿までの反映が遅くなる
上、短時間に大量リクエストを送るとYahoo! Finance側のレート制限（ブロック）に
かかりやすい。

そこで本バージョンから処理を2段階に分割した:

  1. collector.py（本ファイル）：深夜2:00〜6:00 JSTの間に、意図的にゆっくり
     ペースを配分しながら全銘柄のデータ取得・判定を行い、結果を
     data/latest_scan.json に保存する（Discordへは投稿しない）。
  2. poster.py：朝7:00〜8:00 JSTに、保存済みのJSONを読み込んで整形し、
     Discordの5チャネルに投稿する（ネットワーク負荷はほぼゼロ）。

【なぜデータが「古くならない」か】
東証の取引時間は 9:00〜15:30 JST。前日の取引終了（15:30 JST）から翌営業日の
寄り付き（9:00 JST）までは株価・出来高などのデータは更新されないため、
深夜2:00に取得しても朝7:00に取得しても、Yahoo! Finance側の値は基本的に同じ
（＝前日終値ベース）になる。したがって収集タイミングを早めても情報が古くなる
デメリットは実質的に生じない。

【ペーシングの考え方】
【2026-09-04変更】ファンダメンタルズ取得（PER/PBR/配当利回り等）は、1銘柄ずつ
順番に取得し、間にFETCH_DELAY_MIN/MAX_SECONDS（既定1.5〜2.5秒）のランダムな
待機を挟む方式にした。3,900銘柄・平均2秒/銘柄なら理論値で約130分。以前の
「30銘柄を4並列で取得→チャンクごとにまとめてsleep」という方式は、チャンクの
境目でバースト的なアクセスになりやすく、ランダムな間隔で1件ずつ問い合わせる
方が人間の操作に近く、機械的な連続アクセスとして検知されにくいと考えられる
（yfinanceコミュニティで一般的に推奨される方法）。ただしYahoo! Finance側は
制限値を公表していないため、これが「絶対安全」という保証はない。
株価履歴取得（fetch_price_histories）は引き続きHISTORY_WINDOW_MINUTES（既定65分）
に均等に引き延ばす方式のまま（util.pace_to_target、複数銘柄をyf.download()で
バッチ取得するため性質が異なる）。
これとは別に、想定外に遅れた場合の非常停止ラインとして TIME_BUDGET_MINUTES
（既定230分）を設けており、超過分は打ち切ってそこまでの結果を保存する。

【2026-09-04追加：対象銘柄を絞ることによる時間短縮】
上記のペーシング調整に加え、対象銘柄そのものを絞ることでも所要時間・
リクエスト数を削減する。
  ・時価総額フィルタ：日曜（FULL_UNIVERSE_WEEKDAY、既定=6）は全銘柄を対象に
    取得し、その結果から時価総額の下位MARKETCAP_EXCLUDE_BOTTOM_PCT%（既定25%）
    と上位MARKETCAP_EXCLUDE_TOP_PCT%（既定10%）を除外リストとして
    data/marketcap_exclusion.json に保存する。月〜土曜はこのリストを使って
    対象銘柄を先に絞り込む（＝中間65%のみを対象にする）。時価総額は取得済み
    fundamentals情報の一部（marketCap）であり、これを取るための追加リクエストは
    発生しない。除外は「前回の日曜時点の時価総額」に基づく1週間遅れの判定である点、
    上位10%除外は大型・高配当の安定株を意図せず除くトレードオフがある点に注意。
  ・株価フィルタ：1株あたりの現在値がMAX_SHARE_PRICE（既定20,000円）以上の
    銘柄は、値がさ株で個人が買いにくいという実務上の理由から除外する。
    時価総額フィルタと違い、当日の取得結果からその場で判定できるため
    翌週を待たずこの実行から即座に反映される。
  ・いずれの除外リストも読み込みに失敗した場合（未作成・壊れている）は
    安全側に倒して「除外なし＝全銘柄を対象」にフォールバックする。

【正直な注意点】
このスクリプトを開発したサンドボックス環境はネットワークが制限されており、
Yahoo! Finance にも JPX にも直接アクセスできなかったため、実データに対して
通しで動作確認ができていません。必ずテスト用サーバー・MAX_TICKERSを絞った状態
で一度動かし、ログとdata/latest_scan.jsonの中身を確認してください。
特に2026-09-04のペーシング変更は「Yahoo側の制限値が非公開」という前提の上での
経験的な調整のため、MAX_TICKERSを絞った実行で失敗件数（429エラー等）が
増えていないかを必ず確認すること。
"""

import json
import os
import random
import sys
import time
from datetime import datetime, timezone, timedelta

import pandas as pd
import yfinance as yf

from indicators import compute_technical_snapshot, technical_score
from sakata import detect_all_patterns, sakata_score
from scoring import composite_score
from universe import get_all_tse_tickers, FALLBACK_TICKERS
from util import env_int, env_float, json_default, pace_to_target, safe_num
from tracking import (load_trade_log, save_trade_log, open_new_positions,
                       update_open_positions, compute_performance_stats)

# ---------------------------------------------------------------------------
# 実行パラメータ（環境変数で調整可能）
# ---------------------------------------------------------------------------
MAX_TICKERS = env_int('MAX_TICKERS', 0)                       # 0 = 制限なし（全銘柄）
HISTORY_PERIOD = os.getenv('HISTORY_PERIOD') or '9mo'         # MA75/MACDに必要な日数を確保

# 非常停止ライン（想定外に処理が遅れた場合、ここで強制的に打ち切る）
TIME_BUDGET_MINUTES = env_float('TIME_BUDGET_MINUTES', 230)

# 【2026-09-04変更】ファンダメンタルズ取得（fetch_fundamentals）は「チャンク単位で
# まとめて待機」する方式から「1銘柄ずつ、ランダムな間隔で待機」する方式に変更した。
# 詳細はfetch_fundamentals()のdocstring参照。FETCH_DELAY_MIN/MAX_SECONDSがその間隔。
FETCH_DELAY_MIN_SECONDS = env_float('FETCH_DELAY_MIN_SECONDS', 1.5)
FETCH_DELAY_MAX_SECONDS = env_float('FETCH_DELAY_MAX_SECONDS', 2.5)
PROGRESS_LOG_INTERVAL = env_int('PROGRESS_LOG_INTERVAL', 100)  # 何銘柄ごとに進捗ログを出すか

# 株価履歴（fetch_price_histories）は引き続き「チャンク単位でまとめて待機」方式のまま
# （yf.download()で複数銘柄をバッチ取得するため、fetch_fundamentalsとは性質が異なる）。
HISTORY_WINDOW_MINUTES = env_float('HISTORY_WINDOW_MINUTES', 65)

# 上のペーシング時間が「本番実行」として想定している銘柄数の目安（東証全体・約3900銘柄）。
# テスト実行などで対象銘柄数がこれより少ない場合、ペーシング時間を比例配分で短縮する。
REFERENCE_UNIVERSE_SIZE = env_int('REFERENCE_UNIVERSE_SIZE', 3900)

HISTORY_CHUNK_SIZE = env_int('HISTORY_CHUNK_SIZE', 100)
TOP_NEUTRAL_KEEP = env_int('TOP_NEUTRAL_KEEP', 20)             # ニュートラルのうち詳細を残す上位件数
OUTPUT_PATH = os.getenv('SCAN_OUTPUT_PATH') or 'data/latest_scan.json'

# 【2026-09-04追加】時価総額の上下極端な銘柄を除外（GitHub Actions実行時間・リクエスト数
# 削減のため）。下位MARKETCAP_EXCLUDE_BOTTOM_PCT%（小型・薄商い銘柄）と、
# 上位MARKETCAP_EXCLUDE_TOP_PCT%（超大型株、競合が多く情報が出尽くしている領域）の
# 両方を除外し、中間層だけを対象にする。
# 除外リストは「全銘柄を対象にした実行」の結果からのみ更新する（そうでないと、既に絞られた
# 母集団の中だけでパーセンタイルを計算することになり、ランキングが歪む）。
# FULL_UNIVERSE_WEEKDAYの曜日（Python の datetime.weekday() 基準：0=月〜6=日、既定6=日曜）
# だけは除外リストを使わず全銘柄を対象にし、そこで得た時価総額でリストを更新する。
# 初回（除外リストがまだ存在しない場合）も安全側に倒して全銘柄を対象にする。
MARKETCAP_EXCLUSION_PATH = os.getenv('MARKETCAP_EXCLUSION_PATH') or 'data/marketcap_exclusion.json'
MARKETCAP_EXCLUDE_BOTTOM_PCT = env_float('MARKETCAP_EXCLUDE_BOTTOM_PCT', 25.0)  # 下位何%を除外するか
MARKETCAP_EXCLUDE_TOP_PCT = env_float('MARKETCAP_EXCLUDE_TOP_PCT', 10.0)       # 上位何%を除外するか
FULL_UNIVERSE_WEEKDAY = env_int('FULL_UNIVERSE_WEEKDAY', 6)  # 6=日曜（datetime.weekday()基準）

# 【2026-09-04追加】1株あたり株価が高すぎる銘柄の除外（買いにくい・値がさ株を除く実務的な
# フィルタ）。こちらは時価総額と違い、当日の取得結果からその場で判定できるため、
# 除外リストを介さず毎晩即座に反映される。
MAX_SHARE_PRICE = env_float('MAX_SHARE_PRICE', 20000.0)

JST = timezone(timedelta(hours=9))

_START_TIME = time.time()


def _elapsed_minutes():
    return (time.time() - _START_TIME) / 60.0


def _over_budget():
    return _elapsed_minutes() > TIME_BUDGET_MINUTES


def scaled_window_minutes(base_minutes, ticker_count, reference=REFERENCE_UNIVERSE_SIZE, min_minutes=1.0):
    """
    ペーシングの目標時間（base_minutes）を、対象銘柄数（ticker_count）が
    本番想定件数（reference、既定3900）よりどれだけ少ないかに応じて比例配分で短縮する。

    例：base_minutes=150分、reference=3900件のとき、
        - 対象50件（MAX_TICKERSでのテスト実行）なら 150 * 50/3900 ≒ 1.9分
        - 対象3900件（全銘柄・本番実行）なら 150分（そのまま）
        - 対象がreferenceを超える場合も base_minutes を上限として扱う（それ以上は伸ばさない）

    これが無いと、MAX_TICKERSで件数を絞った「数分で終わるはずのテスト実行」でも、
    本番と同じ150分・65分という時間をかけてペーシングしてしまい、テストの意味が薄れる。
    """
    if reference <= 0 or ticker_count <= 0:
        return base_minutes
    scaled = base_minutes * (ticker_count / reference)
    return max(min_minutes, min(base_minutes, scaled))


# ---------------------------------------------------------------------------
# EPS3期推移（LONG候補のみ深掘り取得）
# ---------------------------------------------------------------------------
def get_eps_trend(stock):
    """直近の年次EPSが3期連続で維持〜増加しているかを判定。取得不可ならNone。"""
    try:
        stmt = stock.income_stmt
        if stmt is None or stmt.empty:
            return None

        eps_row = None
        for label in ['Diluted EPS', 'Basic EPS']:
            if label in stmt.index:
                eps_row = stmt.loc[label]
                break
        if eps_row is None:
            candidates = [idx for idx in stmt.index if 'eps' in str(idx).lower()]
            if candidates:
                eps_row = stmt.loc[candidates[0]]
        if eps_row is None:
            return None

        eps_row = eps_row.dropna()
        if len(eps_row) < 3:
            return None

        cols_sorted = sorted(eps_row.index)
        values = [float(eps_row[c]) for c in cols_sorted][-3:]
        increasing = all(values[i] <= values[i + 1] for i in range(len(values) - 1))
        return {'values': values, 'increasing': increasing}
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 時価総額による除外リスト（GitHub Actions実行時間・リクエスト数削減のため）
# ---------------------------------------------------------------------------
def is_full_universe_day(now=None):
    """今日がFULL_UNIVERSE_WEEKDAY（既定：日曜）かどうかをJST基準で判定する。"""
    now = (now or datetime.now(timezone.utc)).astimezone(JST)
    return now.weekday() == FULL_UNIVERSE_WEEKDAY


def load_marketcap_exclusion():
    """除外リストを読み込む。存在しない・壊れている場合は空集合を返す（＝除外なし＝安全側）。"""
    if not os.path.exists(MARKETCAP_EXCLUSION_PATH):
        return set()
    try:
        with open(MARKETCAP_EXCLUSION_PATH, encoding='utf-8') as f:
            data = json.load(f)
        return set(data.get('excluded_tickers', []))
    except Exception as e:
        print(f'[marketcap] 除外リストの読み込みに失敗したため、除外なしで続行します: {e}')
        return set()


def compute_and_save_marketcap_exclusion(fundamentals):
    """
    全銘柄を対象にした実行（既定：日曜）の結果から、時価総額の下位
    MARKETCAP_EXCLUDE_BOTTOM_PCT%・上位MARKETCAP_EXCLUDE_TOP_PCT%に該当する銘柄を
    除外リストとして保存する。次回以降の非全銘柄実行（月〜土想定）は、このリストを
    使って対象銘柄を先に絞り込む。
    """
    caps = [(t, f.get('market_cap')) for t, f in fundamentals.items() if f.get('market_cap')]
    if len(caps) < 10:  # サンプルが少なすぎる場合は信頼できないため更新しない（テスト実行等）
        print(f'[marketcap] 時価総額が取得できた銘柄が{len(caps)}件と少なすぎるため、除外リストを更新しません')
        return

    caps.sort(key=lambda x: x[1])
    n = len(caps)
    bottom_cut = int(n * MARKETCAP_EXCLUDE_BOTTOM_PCT / 100)
    top_cut = int(n * MARKETCAP_EXCLUDE_TOP_PCT / 100)
    bottom_tickers = [t for t, _ in caps[:bottom_cut]]
    top_tickers = [t for t, _ in caps[n - top_cut:]] if top_cut > 0 else []
    excluded = bottom_tickers + top_tickers

    out = {
        'computed_at_utc': datetime.now(timezone.utc).isoformat(),
        'universe_size': n,
        'exclude_bottom_pct': MARKETCAP_EXCLUDE_BOTTOM_PCT,
        'exclude_top_pct': MARKETCAP_EXCLUDE_TOP_PCT,
        'excluded_count': len(excluded),
        'excluded_tickers': excluded,
    }
    out_dir = os.path.dirname(MARKETCAP_EXCLUSION_PATH)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    tmp_path = MARKETCAP_EXCLUSION_PATH + '.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, MARKETCAP_EXCLUSION_PATH)
    print(f'[marketcap] 除外リストを更新しました：{len(excluded)}/{n}銘柄を除外'
          f'（下位{MARKETCAP_EXCLUDE_BOTTOM_PCT}% + 上位{MARKETCAP_EXCLUDE_TOP_PCT}%）')


# ---------------------------------------------------------------------------
# 銘柄ユニバース・株価履歴・ファンダメンタルズの取得
# ---------------------------------------------------------------------------
def resolve_universe():
    tickers, meta_df = get_all_tse_tickers()
    used_fallback = (meta_df is None and tickers == FALLBACK_TICKERS)

    full_universe_today = is_full_universe_day()
    if full_universe_today:
        print('[marketcap] 本日は全銘柄対象日のため、除外リストを使わず時価総額ランキングを更新します')
    else:
        excluded = load_marketcap_exclusion()
        if excluded:
            before = len(tickers)
            tickers = [t for t in tickers if t not in excluded]
            print(f'[marketcap] 時価総額の除外リストにより対象銘柄を{before}→{len(tickers)}件に絞り込みました')
        else:
            print('[marketcap] 除外リストが未作成のため、本日は全銘柄を対象にします')

    if MAX_TICKERS and MAX_TICKERS > 0 and len(tickers) > MAX_TICKERS:
        print(f'[universe] MAX_TICKERS={MAX_TICKERS} が設定されているため先頭{MAX_TICKERS}銘柄に絞り込みます（テスト用）')
        tickers = tickers[:MAX_TICKERS]
    return tickers, meta_df, used_fallback, full_universe_today


def fetch_price_histories(tickers, period=HISTORY_PERIOD, chunk_size=HISTORY_CHUNK_SIZE,
                           max_retries=2, window_minutes=HISTORY_WINDOW_MINUTES):
    """株価履歴（OHLCV）をチャンク単位でバッチ取得する。チャンクごとにペーシングをかける。"""
    histories = {}
    failed = []
    total_chunks = (len(tickers) + chunk_size - 1) // chunk_size
    phase_start = time.time()
    target_seconds = window_minutes * 60

    for ci in range(total_chunks):
        if _over_budget():
            remaining = tickers[ci * chunk_size:]
            print(f'[history] 時間予算（{TIME_BUDGET_MINUTES}分）超過のため残り{len(remaining)}銘柄の株価取得をスキップします')
            failed.extend([(t, '時間予算超過によりスキップ') for t in remaining])
            break

        chunk = tickers[ci * chunk_size:(ci + 1) * chunk_size]
        data = None
        for attempt in range(max_retries):
            try:
                data = yf.download(tickers=chunk, period=period, interval='1d',
                                    group_by='ticker', threads=True, progress=False,
                                    auto_adjust=False)
                break
            except Exception as e:
                print(f'[history] チャンク{ci + 1}/{total_chunks} 取得失敗（試行{attempt + 1}）: {e}')
                if attempt < max_retries - 1:
                    time.sleep(3 * (attempt + 1))

        if data is None:
            failed.extend([(t, '株価データ取得失敗（チャンク単位）') for t in chunk])
        else:
            for t in chunk:
                try:
                    if isinstance(data.columns, pd.MultiIndex):
                        top_level = data.columns.get_level_values(0)
                        if t not in top_level:
                            failed.append((t, '株価データなし'))
                            continue
                        df_t = data[t].dropna(how='all')
                    else:
                        df_t = data.dropna(how='all')

                    if df_t is None or df_t.empty or 'Close' not in df_t.columns:
                        failed.append((t, '株価データなし'))
                        continue
                    histories[t] = df_t
                except Exception as e:
                    failed.append((t, f'株価データ処理エラー: {e}'))

        print(f'[history] 進捗 {min((ci + 1) * chunk_size, len(tickers))}/{len(tickers)}'
              f'（成功{len(histories)}／失敗{len(failed)}） 経過時間 {_elapsed_minutes():.1f}分')

        pace_to_target(ci, total_chunks, phase_start, target_seconds)

    return histories, failed


def _fetch_one_fundamental(ticker, max_retries=2):
    for attempt in range(max_retries):
        try:
            stock = yf.Ticker(ticker)
            info = stock.info or {}
            current_price = info.get('currentPrice') or info.get('regularMarketPrice') or 0
            # 【2026-08-26の本番実行で判明した不具合への対応】特定の銘柄（優先株・ETN・
            # 新しい英数字ティッカーコード等）では、yfinanceのinfoが本来float/intのはず
            # の項目をリスト等で返すことがあり、後段の「PER×PBR」等の掛け算で
            # 「can't multiply sequence by non-int of type 'float'」というTypeErrorに
            # なり該当銘柄の判定処理全体が失敗していた。safe_num()で数値以外はNone
            # （未取得扱い）に丸めてから使うことで、その銘柄だけ判定不能・0点扱いに
            # 留め、収集処理全体は継続できるようにする。
            return ticker, {
                'name': info.get('longName') or info.get('shortName') or ticker,
                'current_price': safe_num(current_price) or 0,
                # 【Phase1の修正を踏襲】yfinanceのdividendYieldはパーセント値そのもの
                # （3.5のような値）として返るため *100 しない。
                'dividend_yield': safe_num(info.get('dividendYield', 0)) or 0,
                'pbr': safe_num(info.get('priceToBook', 0)) or 0,
                'per': safe_num(info.get('trailingPE', 0)) or 0,
                'growth_raw': safe_num(info.get('earningsQuarterlyGrowth', None)),
                'sector': info.get('sector'),
                # 【2026-09-04追加】時価総額下位銘柄の除外判定に使う。.info呼び出しに
                # 元々含まれているフィールドなので、追加のリクエストは発生しない。
                'market_cap': safe_num(info.get('marketCap')),
            }, None
        except Exception as e:
            if attempt == max_retries - 1:
                return ticker, None, str(e)
            time.sleep(1 + attempt)
    return ticker, None, '不明なエラー'


def fetch_fundamentals(tickers, delay_min=FETCH_DELAY_MIN_SECONDS, delay_max=FETCH_DELAY_MAX_SECONDS,
                        log_interval=PROGRESS_LOG_INTERVAL):
    """
    配当利回り・PER・PBR等をticker.info経由で取得する。

    【2026-09-04変更の背景】以前は「30銘柄を4並列で取得→チャンク完了ごとに
    まとめてsleep」という設計だった。これはチャンクの境目でバースト的な
    アクセス（短時間に複数リクエストが集中）が発生しやすく、Yahoo! Finance側の
    レート制限（非公式・仕様非公開）に対してはむしろ不利な可能性がある。

    そこで「1銘柄ずつ順番に取得し、間に1.5〜2.5秒程度のランダムな待機を挟む」
    方式に変更した。一定間隔ではなくランダムにばらつかせることで、人間が
    1件ずつ確認しているようなアクセスパターンに近づけ、機械的な連続アクセスとして
    検知されにくくすることを狙っている（yfinanceコミュニティで一般的に推奨される
    「リクエスト間に1〜3秒のランダムな待機を入れる」という方法に準拠）。

    3,900銘柄・平均2秒/銘柄なら理論値で約130分（実際の通信時間も乗るため、
    実測ではこれより長くなる）。この理論値には根拠となる「絶対安全な数値」は
    存在しない（Yahoo側が制限値を公表していないため）ので、必ずMAX_TICKERSを
    絞った小規模テストで失敗件数を確認してから本番投入すること。
    """
    fundamentals = {}
    failed = []
    total = len(tickers)

    for i, ticker in enumerate(tickers):
        if _over_budget():
            remaining = tickers[i:]
            print(f'[fundamentals] 時間予算（{TIME_BUDGET_MINUTES}分）超過のため残り{len(remaining)}銘柄をスキップします')
            failed.extend([(t, '時間予算超過によりスキップ') for t in remaining])
            break

        _, data, err = _fetch_one_fundamental(ticker)
        if data is not None:
            fundamentals[ticker] = data
        else:
            failed.append((ticker, err or '取得失敗'))

        if (i + 1) % log_interval == 0 or (i + 1) == total:
            print(f'[fundamentals] 進捗 {i + 1}/{total}（成功{len(fundamentals)}／失敗{len(failed)}）'
                  f' 経過時間 {_elapsed_minutes():.1f}分')

        if i < total - 1:  # 最後の1件の後は待つ必要が無い
            time.sleep(random.uniform(delay_min, delay_max))

    return fundamentals, failed


# ---------------------------------------------------------------------------
# 銘柄ごとの判定ロジック（ファンダメンタルズ＋テクニカル＋酒田五法）
# ---------------------------------------------------------------------------
def analyze_ticker(ticker, fund, price_df):
    """
    1銘柄分の判定を行う。

    LONG/SHORTの一次振り分け条件（kurosuke割安チェッカーの根幹）は変更していない:
      LONG  : 配当利回り 3.5〜5.8% かつ PER×PBR ≦ 22.5
      SHORT : PER×PBR > 30（割高の目安）、または
              MA配列が弱気かつ精度70%以上の弱気酒田五法パターンを検出
    """
    per = fund.get('per', 0) or 0
    pbr = fund.get('pbr', 0) or 0
    div = fund.get('dividend_yield', 0) or 0
    per_pbr = per * pbr if per and pbr else None

    dividend_ok = bool(div) and 3.5 <= div <= 5.8
    per_pbr_ok = per_pbr is not None and per_pbr <= 22.5
    super_cheap = per_pbr is not None and per_pbr < 15

    tech_snapshot = compute_technical_snapshot(price_df)
    tech_score_val, tech_reasons = technical_score(tech_snapshot)

    patterns = detect_all_patterns(price_df)
    sak_score_val, sak_reasons = sakata_score(patterns)

    score, breakdown, avail_ratio = composite_score(
        dividend_yield=div if div else None,
        per_pbr=per_pbr,
        eps_trend=None,
        technical_score_val=tech_score_val,
        sakata_score_val=sak_score_val,
        growth_value=fund.get('growth_raw'),
    )

    ma_trend = (tech_snapshot.get('ma') or {}).get('trend')
    bearish_pattern_strong = any(
        p['detected'] and p['direction'] == 'bearish' and (p['reference_accuracy'] or 0) >= 70
        for p in patterns
    )
    bullish_pattern_strong = any(
        p['detected'] and p['direction'] == 'bullish' and (p['reference_accuracy'] or 0) >= 70
        for p in patterns
    )

    if dividend_ok and per_pbr_ok:
        signal = 'LONG'
    elif (per_pbr is not None and per_pbr > 30) or (ma_trend == 'bearish' and bearish_pattern_strong):
        signal = 'SHORT'
    else:
        signal = 'NEUTRAL'

    entry_timing = None
    if signal == 'LONG':
        good_timing = (tech_score_val or 0) >= 0.5 and not bearish_pattern_strong
        entry_timing = 'good' if good_timing else 'wait'

    return {
        'ticker': ticker,
        'name': fund.get('name'),
        'current_price': fund.get('current_price'),
        'market_cap': fund.get('market_cap'),
        'dividend_yield': div,
        'per_pbr': per_pbr,
        'super_cheap': super_cheap,
        'dividend_ok': dividend_ok,
        'per_pbr_ok': per_pbr_ok,
        'tech_snapshot': tech_snapshot,
        'tech_reasons': tech_reasons,
        'patterns': patterns,
        'sakata_reasons': sak_reasons,
        'bullish_pattern_strong': bullish_pattern_strong,
        'bearish_pattern_strong': bearish_pattern_strong,
        'score': score,
        'score_breakdown': breakdown,
        'score_available_ratio': avail_ratio,
        'signal': signal,
        'entry_timing': entry_timing,
        'growth_raw': fund.get('growth_raw'),
        'eps_trend': None,
    }


def run_eps_deepdive(results):
    """LONG候補についてのみ income_stmt を追加取得し、EPS3期推移をスコアに反映する。"""
    long_tickers = [r['ticker'] for r in results.values() if r['signal'] == 'LONG']
    if not long_tickers:
        return

    print(f'[eps-deepdive] LONG候補{len(long_tickers)}銘柄についてEPS3期推移を追加取得します')
    for ticker in long_tickers:
        if _over_budget():
            print('[eps-deepdive] 時間予算超過のため以降のEPS取得を打ち切ります')
            break
        try:
            stock = yf.Ticker(ticker)
            eps_trend = get_eps_trend(stock)
        except Exception as e:
            print(f'[eps-deepdive] {ticker} のEPS取得に失敗: {e}')
            eps_trend = None
        time.sleep(0.3)  # LONG候補は数十〜数百件程度のはずなので、軽く間隔を空ける程度で十分

        r = results[ticker]
        r['eps_trend'] = eps_trend
        score, breakdown, avail_ratio = composite_score(
            dividend_yield=r['dividend_yield'] if r['dividend_yield'] else None,
            per_pbr=r['per_pbr'],
            eps_trend=eps_trend,
            technical_score_val=r['score_breakdown'].get('technical'),
            sakata_score_val=r['score_breakdown'].get('sakata'),
            growth_value=r['growth_raw'],
        )
        r['score'] = score
        r['score_breakdown'] = breakdown
        r['score_available_ratio'] = avail_ratio


# ---------------------------------------------------------------------------
# 結果の保存（LONG/SHORT/上位ニュートラルのみ詳細を残し、それ以外は要約のみにして
# JSONファイルのサイズを抑える）
# ---------------------------------------------------------------------------
def serialize_results(results):
    neutrals = sorted(
        [r for r in results.values() if r['signal'] == 'NEUTRAL'],
        key=lambda r: (r['score'] if r['score'] is not None else -1), reverse=True,
    )
    top_neutral_tickers = {r['ticker'] for r in neutrals[:TOP_NEUTRAL_KEEP]}

    out = {}
    for ticker, r in results.items():
        if r['signal'] in ('LONG', 'SHORT') or ticker in top_neutral_tickers:
            entry = dict(r)
            entry['detail'] = 'full'
        else:
            entry = {
                'ticker': r['ticker'], 'name': r['name'], 'current_price': r['current_price'],
                'dividend_yield': r['dividend_yield'], 'per_pbr': r['per_pbr'],
                'score': r['score'], 'signal': r['signal'], 'detail': 'slim',
            }
        out[ticker] = entry
    return out


def build_snapshot(results, fund_failed, hist_failed, tickers, used_fallback, started_at_utc,
                    performance_stats=None):
    counts = {'LONG': 0, 'SHORT': 0, 'NEUTRAL': 0}
    for r in results.values():
        counts[r['signal']] = counts.get(r['signal'], 0) + 1
    counts['analyzed'] = len(results)

    now_utc = datetime.now(timezone.utc)
    return {
        'schema_version': 1,
        'generated_at_utc': now_utc.isoformat(),
        'started_at_utc': started_at_utc.isoformat(),
        'universe_size': len(tickers),
        'used_fallback': used_fallback,
        'elapsed_minutes': round(_elapsed_minutes(), 1),
        'counts': counts,
        'fund_failed': fund_failed,
        'hist_failed': hist_failed,
        'results': serialize_results(results),
        'performance_stats': performance_stats,
    }


def write_snapshot(snapshot, path=None):
    """
    【注意】デフォルト引数を path=OUTPUT_PATH とすると関数定義時点の値に固定され、
    モジュール属性 collector.OUTPUT_PATH を後から書き換えても反映されない
    （テストでのmock.patch.object等）。呼び出し時に毎回モジュールグローバルを
    参照する形にしている。
    """
    if path is None:
        path = OUTPUT_PATH
    out_dir = os.path.dirname(path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    # 書き込み途中でジョブが落ちても壊れたJSONを残さないよう、一時ファイル経由でrenameする
    tmp_path = path + '.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(snapshot, f, ensure_ascii=False, default=json_default)
    os.replace(tmp_path, path)
    print(f'[collector] 結果を書き込みました: {path}')


# ---------------------------------------------------------------------------
# メイン処理
# ---------------------------------------------------------------------------
def collect():
    started_at_utc = datetime.now(timezone.utc)
    print("=" * 80)
    print("Kurosuke データ収集バッチ（collector.py）")
    print(f"開始時刻（UTC）：{started_at_utc.isoformat()}")
    print(f"非常停止ライン：{TIME_BUDGET_MINUTES}分／"
          f"株価履歴目標配分：{HISTORY_WINDOW_MINUTES}分")
    print("=" * 80)

    tickers, meta_df, used_fallback, full_universe_today = resolve_universe()
    print(f"対象銘柄数：{len(tickers)}" + ("（フォールバック銘柄）" if used_fallback else ""))

    if not tickers:
        print("対象銘柄が0件のため処理を中断します")
        return False

    # 【2026-09-04変更】ファンダメンタルズは銘柄数に応じたペーシング配分（旧scaled_window_minutes）
    # ではなく、1銘柄あたり固定のランダム待機（FETCH_DELAY_MIN/MAX_SECONDS）に変更したため、
    # ここでの所要時間見積もりは概算（平均待機時間×銘柄数）で表示する。
    fund_estimate_minutes = len(tickers) * (FETCH_DELAY_MIN_SECONDS + FETCH_DELAY_MAX_SECONDS) / 2 / 60
    print(f"\n--- ファンダメンタルズ取得（1銘柄ずつ{FETCH_DELAY_MIN_SECONDS}〜{FETCH_DELAY_MAX_SECONDS}秒のランダム待機、"
          f"所要時間の目安 約{fund_estimate_minutes:.1f}分） ---")
    fundamentals, fund_failed = fetch_fundamentals(tickers)

    # 【2026-09-04】従来はyfinanceのinfo['longName']（英語社名）で'name'を常に
    # 上書きしていたため、universe.py がJPXの銘柄一覧から取得済みの日本語社名
    # （meta_df の'name'列）が使われず捨てられていた。ここでmeta_df の日本語社名が
    # あれば優先的に採用し、取得できていない銘柄のみyfinanceの英語名のまま残す。
    if meta_df is not None:
        jp_name_by_ticker = {
            row['ticker']: str(row['name']).strip()
            for row in meta_df.to_dict('records')
            # meta_df由来の欠損値はNoneではなくpandasのNaN(float)で来るため、
            # 単純なtruthy判定だと文字列"nan"がそのまま社名として採用されてしまう。
            # pd.notna()で明示的に弾く。
            if pd.notna(row.get('name')) and str(row['name']).strip()
        }
        overridden = 0
        for ticker, fund in fundamentals.items():
            jp_name = jp_name_by_ticker.get(ticker)
            if jp_name:
                fund['name'] = jp_name
                overridden += 1
        print(f"[universe] 日本語社名を{overridden}/{len(fundamentals)}銘柄に反映"
              f"（残りはJPX一覧に社名が無くyfinanceの英語名のまま）")

    # 【2026-09-04追加】1株あたり株価が高すぎる銘柄を除外する（買いにくい値がさ株を除く
    # 実務的フィルタ。時価総額の除外と違い、当日の取得結果からその場で判定できるため
    # 即座に反映される＝この実行のok_tickers/後続処理から即座に除かれる）。
    high_price_tickers = [
        t for t, f in fundamentals.items()
        if f.get('current_price') and f['current_price'] >= MAX_SHARE_PRICE
    ]
    for t in high_price_tickers:
        fundamentals.pop(t)
        fund_failed.append((t, f'株価が{MAX_SHARE_PRICE:,.0f}円以上のため除外'))
    if high_price_tickers:
        print(f"[filter] 1株{MAX_SHARE_PRICE:,.0f}円以上のため{len(high_price_tickers)}銘柄を除外しました")

    # 【2026-09-04追加】全銘柄対象日（既定：日曜）なら、ここで得た時価総額を使って
    # 除外リストを更新する（次回以降の非全銘柄実行で使われる）。
    if full_universe_today:
        compute_and_save_marketcap_exclusion(fundamentals)

    ok_tickers = list(fundamentals.keys())
    hist_window = scaled_window_minutes(HISTORY_WINDOW_MINUTES, len(ok_tickers))
    print(f"\n--- 株価履歴取得（テクニカル・酒田五法用、ペーシングあり、目標配分 {hist_window:.1f}分） ---")
    histories, hist_failed = fetch_price_histories(ok_tickers, window_minutes=hist_window)

    if not fundamentals:
        print("全銘柄のファンダメンタルズ取得に失敗しました。空の結果を保存して終了します。")
        snapshot = build_snapshot({}, fund_failed, hist_failed, tickers, used_fallback, started_at_utc)
        write_snapshot(snapshot)
        return False

    print("\n--- 銘柄ごとの判定（ファンダメンタルズ＋テクニカル＋酒田五法） ---")
    results = {}
    for ticker, fund in fundamentals.items():
        try:
            results[ticker] = analyze_ticker(ticker, fund, histories.get(ticker))
        except Exception as e:
            print(f"[analyze] {ticker} の判定でエラー: {e}")
            fund_failed.append((ticker, f'判定処理エラー: {e}'))

    print("\n--- LONG候補のEPS3期推移 深掘り取得 ---")
    run_eps_deepdive(results)

    # MAX_TICKERSを絞ったテスト実行では、対象銘柄が本番と異なる一部分になり、
    # 本番用の勝率・ペイオフレシオ集計（data/trade_log.json）にノイズが混ざって
    # しまうため、テスト実行時は追跡をスキップする（画面表示は「集計対象外」とする）。
    is_test_run = bool(MAX_TICKERS and MAX_TICKERS > 0)
    if is_test_run:
        print("\n--- 仮想ポジション追跡：テスト実行（MAX_TICKERS指定）のためスキップします ---")
        performance_stats = None
    else:
        print("\n--- 仮想ポジション追跡（勝率・ペイオフレシオ集計用、実際の取引ではありません） ---")
        today_str = started_at_utc.strftime('%Y-%m-%d')
        trade_log = load_trade_log()
        closed_n = update_open_positions(trade_log, results, today_str)
        opened_n = open_new_positions(trade_log, results, today_str)
        save_trade_log(trade_log)
        performance_stats = compute_performance_stats(trade_log)
        print(f"[tracking] 新規建玉{opened_n}件／決済{closed_n}件／保有中{performance_stats['open_positions']}件")

    snapshot = build_snapshot(results, fund_failed, hist_failed, tickers, used_fallback, started_at_utc,
                               performance_stats=performance_stats)
    write_snapshot(snapshot)

    print(f"\n総実行時間：{_elapsed_minutes():.1f}分")
    print(f"内訳：LONG {snapshot['counts'].get('LONG', 0)} ／ "
          f"SHORT {snapshot['counts'].get('SHORT', 0)} ／ "
          f"NEUTRAL {snapshot['counts'].get('NEUTRAL', 0)}")
    print("収集完了。Discordへの投稿は poster.py が別途行います。")
    return True


if __name__ == "__main__":
    try:
        success = collect()
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"Error: {str(e)}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
