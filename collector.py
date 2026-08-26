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
一気に全銘柄を並列で叩くのではなく、FUNDAMENTALS_WINDOW_MINUTES /
HISTORY_WINDOW_MINUTES で指定した時間（既定：150分 + 70分 ＝ 220分、
2:00開始なら5:40頃に終わる想定）に均等に引き延ばしながら取得する
（util.pace_to_target）。これとは別に、想定外に遅れた場合の非常停止ライン
として TIME_BUDGET_MINUTES（既定230分）を設けており、超過分は打ち切って
そこまでの結果を保存する。

【正直な注意点】
このスクリプトを開発したサンドボックス環境はネットワークが制限されており、
Yahoo! Finance にも JPX にも直接アクセスできなかったため、実データに対して
通しで動作確認ができていません。必ずテスト用サーバー・MAX_TICKERSを絞った状態
で一度動かし、ログとdata/latest_scan.jsonの中身を確認してください。
"""

import concurrent.futures
import json
import os
import sys
import time
from datetime import datetime, timezone

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

# 意図的にゆっくり進めるためのペーシング目標時間（合計が概ねTIME_BUDGET_MINUTES以下になるように）
# ※ これは「全銘柄（約3900銘柄）を対象にした本番実行」を想定した時間。MAX_TICKERSで
#   件数を絞ったテスト実行では、下の scaled_window_minutes() で対象銘柄数に比例して
#   自動的に短縮する（さもないと50銘柄のテストでも数時間かかってしまうため）。
FUNDAMENTALS_WINDOW_MINUTES = env_float('FUNDAMENTALS_WINDOW_MINUTES', 150)
HISTORY_WINDOW_MINUTES = env_float('HISTORY_WINDOW_MINUTES', 65)

# 上のペーシング時間が「本番実行」として想定している銘柄数の目安（東証全体・約3900銘柄）。
# テスト実行などで対象銘柄数がこれより少ない場合、ペーシング時間を比例配分で短縮する。
REFERENCE_UNIVERSE_SIZE = env_int('REFERENCE_UNIVERSE_SIZE', 3900)

FETCH_WORKERS = env_int('FETCH_WORKERS', 4)                   # 同時並列数（控えめに）
HISTORY_CHUNK_SIZE = env_int('HISTORY_CHUNK_SIZE', 100)
INFO_CHUNK_SIZE = env_int('INFO_CHUNK_SIZE', 30)
TOP_NEUTRAL_KEEP = env_int('TOP_NEUTRAL_KEEP', 20)             # ニュートラルのうち詳細を残す上位件数
OUTPUT_PATH = os.getenv('SCAN_OUTPUT_PATH') or 'data/latest_scan.json'

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
# 銘柄ユニバース・株価履歴・ファンダメンタルズの取得
# ---------------------------------------------------------------------------
def resolve_universe():
    tickers, meta_df = get_all_tse_tickers()
    used_fallback = (meta_df is None and tickers == FALLBACK_TICKERS)
    if MAX_TICKERS and MAX_TICKERS > 0 and len(tickers) > MAX_TICKERS:
        print(f'[universe] MAX_TICKERS={MAX_TICKERS} が設定されているため先頭{MAX_TICKERS}銘柄に絞り込みます（テスト用）')
        tickers = tickers[:MAX_TICKERS]
    return tickers, meta_df, used_fallback


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
            }, None
        except Exception as e:
            if attempt == max_retries - 1:
                return ticker, None, str(e)
            time.sleep(1 + attempt)
    return ticker, None, '不明なエラー'


def fetch_fundamentals(tickers, workers=FETCH_WORKERS, chunk_size=INFO_CHUNK_SIZE,
                        window_minutes=FUNDAMENTALS_WINDOW_MINUTES):
    """配当利回り・PER・PBR等をticker.info経由で取得する（並列・チャンク処理・ペーシングあり）。"""
    fundamentals = {}
    failed = []
    total_chunks = (len(tickers) + chunk_size - 1) // chunk_size
    phase_start = time.time()
    target_seconds = window_minutes * 60

    for ci in range(total_chunks):
        if _over_budget():
            remaining = tickers[ci * chunk_size:]
            print(f'[fundamentals] 時間予算（{TIME_BUDGET_MINUTES}分）超過のため残り{len(remaining)}銘柄をスキップします')
            failed.extend([(t, '時間予算超過によりスキップ') for t in remaining])
            break

        chunk = tickers[ci * chunk_size:(ci + 1) * chunk_size]
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(_fetch_one_fundamental, t) for t in chunk]
            for fut in concurrent.futures.as_completed(futures):
                ticker, data, err = fut.result()
                if data is not None:
                    fundamentals[ticker] = data
                else:
                    failed.append((ticker, err or '取得失敗'))

        done = min((ci + 1) * chunk_size, len(tickers))
        print(f'[fundamentals] 進捗 {done}/{len(tickers)}（成功{len(fundamentals)}／失敗{len(failed)}）'
              f' 経過時間 {_elapsed_minutes():.1f}分')

        pace_to_target(ci, total_chunks, phase_start, target_seconds)

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
          f"ファンダメンタルズ目標配分：{FUNDAMENTALS_WINDOW_MINUTES}分／"
          f"株価履歴目標配分：{HISTORY_WINDOW_MINUTES}分")
    print("=" * 80)

    tickers, meta_df, used_fallback = resolve_universe()
    print(f"対象銘柄数：{len(tickers)}" + ("（フォールバック銘柄）" if used_fallback else ""))

    if not tickers:
        print("対象銘柄が0件のため処理を中断します")
        return False

    fund_window = scaled_window_minutes(FUNDAMENTALS_WINDOW_MINUTES, len(tickers))
    print(f"\n--- ファンダメンタルズ取得（ペーシングあり、目標配分 {fund_window:.1f}分） ---")
    fundamentals, fund_failed = fetch_fundamentals(tickers, window_minutes=fund_window)

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
