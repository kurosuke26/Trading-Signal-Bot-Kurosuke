#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
breaking_alerts.py — 「速報」チャンネル向けの急変動アラート（15分おきに実行する想定）

collector.py/poster.py（1日1回、朝のシグナル投稿）とは別の、日中ずっと動く軽量バッチ。
以下4種類を「速報」チャンネルにのみ投稿する。

  1. 為替（USD/JPY）の急変動：直近チェック（約15分前）比、および本日(JST)の
     累計変化率がしきい値を超えたら投稿。二重通知を防ぐためクールダウンあり。
  2. 日経平均の急変動：仕組みは為替と同じ。
  3. 日銀・FRBの金融政策発表：公式RSSをポーリングし、未読の新着記事があれば投稿。
  4. 米国主要指数（NYダウ・S&P500・ナスダック）の朝の速報：JST 7時台に1日1回だけ、
     前日終値の騰落率をまとめて投稿（しきい値なし。「朝に届けばよい」という運用要望のため）。

【正直な注意点】
- GitHub Actionsのschedule cronは実行時刻が数分ずれることがある前提の設計
  （厳密な時刻一致ではなく「JST 7時台の回」を対象にしている）。
- 為替・日経平均は「前回チェック時からの変化率」を見る設計のため、休場中（夜間・
  週末等）はほぼ変化がなく静かに投稿されない。取引時間外を明示的に除外する処理は
  入れていない（値が動かなければ自然にしきい値を超えないため、実害は無い想定）。
- 状態（前回価格・クールダウン・既読RSS記事）は data/breaking_alert_state.json に
  保存し、collect_data.yml等と同じ「実行のたびにコミットして永続化する」方式を使う。
"""

import json
import os
import sys
from datetime import datetime, timezone, timedelta

import yfinance as yf

import poster
from theme_news import fetch_feed_entries
from util import env_float, json_default

STATE_PATH = os.getenv('BREAKING_ALERT_STATE_PATH') or 'data/breaking_alert_state.json'
LOG_PATH = os.getenv('BREAKING_ALERT_LOG_PATH') or 'data/breaking_alert_log.json'
LOG_RETENTION_DAYS = 35  # weekly_tips.pyの週間振り返り用。1週間分より余裕を持たせて保持
JST = timezone(timedelta(hours=9))

# ---------------------------------------------------------------------------
# しきい値・クールダウン（すべて環境変数で上書き可能。デフォルトは初期値の目安）
# ---------------------------------------------------------------------------
FX_TICKER = 'JPY=X'  # yfinanceでのUSD/JPY
FX_RAPID_THRESHOLD_PCT = env_float('BREAKING_FX_RAPID_THRESHOLD_PCT', 0.5)
FX_DAILY_THRESHOLD_PCT = env_float('BREAKING_FX_DAILY_THRESHOLD_PCT', 1.5)
FX_COOLDOWN_MINUTES = env_float('BREAKING_FX_COOLDOWN_MINUTES', 60)

NIKKEI_TICKER = '^N225'
NIKKEI_RAPID_THRESHOLD_PCT = env_float('BREAKING_NIKKEI_RAPID_THRESHOLD_PCT', 0.8)
NIKKEI_DAILY_THRESHOLD_PCT = env_float('BREAKING_NIKKEI_DAILY_THRESHOLD_PCT', 2.0)
NIKKEI_COOLDOWN_MINUTES = env_float('BREAKING_NIKKEI_COOLDOWN_MINUTES', 60)

# 日銀・FRBの公式RSS（discord-ai-team側の適時開示調査(2026-09)で生存確認済みのURL）
POLICY_FEEDS = [
    {'source': '日銀', 'url': 'https://www.boj.or.jp/rss/whatsnew.xml'},
    {'source': 'FRB', 'url': 'https://www.federalreserve.gov/feeds/press_monetary.xml'},
]
MAX_SEEN_LINKS = 200  # 状態ファイル肥大化防止（各フィードごとの既読リンク保持上限）

US_INDEX_TICKERS = [
    ('^DJI', 'NYダウ'),
    ('^GSPC', 'S&P500'),
    ('^IXIC', 'ナスダック'),
]


# ---------------------------------------------------------------------------
# 状態の読み書き
# ---------------------------------------------------------------------------
def load_state():
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH, encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f'[breaking_alerts] 状態ファイルの読み込みに失敗、初期状態で再開します: {e}', file=sys.stderr)
        return {}


def save_state(state):
    out_dir = os.path.dirname(STATE_PATH)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    tmp_path = STATE_PATH + '.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2, default=json_default)
    os.replace(tmp_path, STATE_PATH)


def load_log():
    if not os.path.exists(LOG_PATH):
        return []
    try:
        with open(LOG_PATH, encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f'[breaking_alerts] ログファイルの読み込みに失敗、空で再開します: {e}', file=sys.stderr)
        return []


def append_to_log(entries, now_utc):
    """
    今回投稿したアラート（(category, text)のリスト）を data/breaking_alert_log.json に
    追記する。weekly_tips.py が「今週あった速報」を短文で振り返るために読む。
    LOG_RETENTION_DAYSより古いものは削除し、ファイルの肥大化を防ぐ。
    """
    if not entries:
        return
    log = load_log()
    for category, text in entries:
        log.append({'at_utc': now_utc.isoformat(), 'category': category, 'text': text})

    cutoff = (now_utc - timedelta(days=LOG_RETENTION_DAYS)).isoformat()
    log = [e for e in log if e.get('at_utc', '') >= cutoff]

    out_dir = os.path.dirname(LOG_PATH)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    tmp_path = LOG_PATH + '.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(log, f, ensure_ascii=False, indent=2, default=json_default)
    os.replace(tmp_path, LOG_PATH)


# ---------------------------------------------------------------------------
# 1・2. 為替／日経平均の急変動チェック（仕組みは共通）
# ---------------------------------------------------------------------------
def fetch_last_price(ticker):
    """yfinanceの軽量な最新値取得。失敗時はNone（呼び出し側で静かにスキップ）。

    【2026-09-24修正】fast_infoの内部辞書キーは'lastPrice'（キャメルケース）で、
    'last_price'という辞書キーは存在しないため.get('last_price')は常にNoneだった
    （属性アクセスのfast_info.last_priceなら正しく取れる）。この結果、FX・日経の
    急変動チェックが1ヶ月以上一度もアラートを出せていなかった。
    """
    try:
        price = yf.Ticker(ticker).fast_info.last_price
        return float(price) if price is not None else None
    except Exception as e:
        print(f'[breaking_alerts] {ticker} の価格取得に失敗: {e}', file=sys.stderr)
        return None


def check_instrument(state, key, ticker, label, rapid_threshold_pct, daily_threshold_pct,
                      cooldown_minutes, now_utc, today_jst_str):
    """
    為替・日経平均共通の急変動チェック。stateを直接更新し、(category, text)のリスト
    （0〜2件）を返す。
    - 「直近チェック比」と「本日(JST)累計」の2種類を独立に判定する
    - クールダウン中は変化率が閾値を超えていてもアラートを送らない（連投防止）
    """
    price = fetch_last_price(ticker)
    if price is None:
        return []

    inst = state.setdefault(key, {})
    results = []

    last_alert_at = inst.get('last_alert_at_utc')
    in_cooldown = False
    if last_alert_at:
        try:
            elapsed_min = (now_utc - datetime.fromisoformat(last_alert_at)).total_seconds() / 60
            in_cooldown = elapsed_min < cooldown_minutes
        except ValueError:
            pass

    # (a) 直近チェック比（約15分前との比較）
    last_price = inst.get('last_price')
    if last_price and not in_cooldown:
        change_pct = (price - last_price) / last_price * 100
        if abs(change_pct) >= rapid_threshold_pct:
            arrow = '📈' if change_pct > 0 else '📉'
            results.append((f'{key}_rapid',
                f'{arrow} **{label} 急変動**：{last_price:,.2f} → {price:,.2f}'
                f'（{change_pct:+.2f}%、直近15分程度）'
            ))

    # (b) 本日(JST)の累計変化（日付が変わったら基準値をリセット）
    if inst.get('day_start_date_jst') != today_jst_str:
        inst['day_start_date_jst'] = today_jst_str
        inst['day_start_price'] = price
    day_start_price = inst.get('day_start_price')
    if day_start_price and not in_cooldown:
        daily_change_pct = (price - day_start_price) / day_start_price * 100
        if abs(daily_change_pct) >= daily_threshold_pct:
            arrow = '📈' if daily_change_pct > 0 else '📉'
            results.append((f'{key}_daily',
                f'{arrow} **{label} 本日大幅変動**：本日{day_start_price:,.2f} → {price:,.2f}'
                f'（{daily_change_pct:+.2f}%、本日累計）'
            ))

    if results:
        inst['last_alert_at_utc'] = now_utc.isoformat()
    inst['last_price'] = price
    inst['last_check_at_utc'] = now_utc.isoformat()
    return results


# ---------------------------------------------------------------------------
# 3. 日銀・FRBの政策発表チェック
# ---------------------------------------------------------------------------
def check_policy_feeds(state):
    seen_by_source = state.setdefault('policy_seen_links', {})
    results = []
    for feed in POLICY_FEEDS:
        source = feed['source']
        is_first_run = source not in seen_by_source
        seen = set(seen_by_source.get(source, []))
        entries = fetch_feed_entries(feed)

        new_entries = [
            e for e in entries
            if (e['link'] or e['title']) and (e['link'] or e['title']) not in seen
        ]
        # 初回実行時はRSS全件が「未読」になってしまうため、投稿は最新1件（フィード
        # 先頭）だけに絞る。ただし「未投稿=未読のまま」にすると、次回実行時に
        # 残り全件がまとめて「新着」扱いされ大量投稿されてしまう
        # （2026-09-14に実際に発生した不具合）。そのため、投稿しない分も含めて
        # 今回取得できた全件を必ずseenへ登録する。
        to_post = new_entries[:1] if is_first_run else new_entries

        for entry in new_entries:
            link = entry['link'] or entry['title']
            seen.add(link)

        for entry in to_post:
            results.append(('policy',
                f'📜 **{source}発表**：{entry["title"]}\n{entry["link"]}'
            ))

        # 既読リンクは最大MAX_SEEN_LINKS件まで保持（無限に肥大化しないように）
        seen_by_source[source] = list(seen)[-MAX_SEEN_LINKS:]
    return results


# ---------------------------------------------------------------------------
# 4. 米国主要指数の朝の速報（JST 7時台に1日1回）
# ---------------------------------------------------------------------------
def check_us_morning_report(state, now_jst, today_jst_str):
    if now_jst.hour != 7:
        return []
    if state.get('us_morning_report_date_jst') == today_jst_str:
        return []  # 本日分は投稿済み

    lines = []
    for ticker, label in US_INDEX_TICKERS:
        try:
            hist = yf.Ticker(ticker).history(period='5d', interval='1d')
            closes = hist['Close'].dropna()
            if len(closes) < 2:
                lines.append(f'{label}：データ取得不可')
                continue
            prev, latest = float(closes.iloc[-2]), float(closes.iloc[-1])
            change_pct = (latest - prev) / prev * 100
            arrow = '📈' if change_pct >= 0 else '📉'
            lines.append(f'{arrow} {label} {change_pct:+.2f}%')
        except Exception as e:
            print(f'[breaking_alerts] {label}({ticker}) の取得に失敗: {e}', file=sys.stderr)
            lines.append(f'{label}：データ取得不可')

    state['us_morning_report_date_jst'] = today_jst_str
    if not lines:
        return []
    return [('us_morning', '🌅 **米国市場サマリー（前日終値比）**\n' + '\n'.join(lines))]


# ---------------------------------------------------------------------------
# メイン処理
# ---------------------------------------------------------------------------
def build_payload(text):
    return {'content': text, 'username': 'Kurosuke速報'}


def run():
    state = load_state()
    now_utc = datetime.now(timezone.utc)
    now_jst = now_utc.astimezone(JST)
    today_jst_str = now_jst.date().isoformat()

    all_results = []
    all_results += check_instrument(
        state, 'usdjpy', FX_TICKER, 'USD/JPY',
        FX_RAPID_THRESHOLD_PCT, FX_DAILY_THRESHOLD_PCT, FX_COOLDOWN_MINUTES,
        now_utc, today_jst_str,
    )
    all_results += check_instrument(
        state, 'nikkei', NIKKEI_TICKER, '日経平均',
        NIKKEI_RAPID_THRESHOLD_PCT, NIKKEI_DAILY_THRESHOLD_PCT, NIKKEI_COOLDOWN_MINUTES,
        now_utc, today_jst_str,
    )
    all_results += check_policy_feeds(state)
    all_results += check_us_morning_report(state, now_jst, today_jst_str)

    save_state(state)

    if not all_results:
        print('[breaking_alerts] 今回は速報対象なしでした')
        return True

    all_ok = True
    for category, text in all_results:
        print(f'[breaking_alerts] 投稿({category}): {text[:60]}...')
        ok = poster.send_discord_message('BREAKING', build_payload(text))
        all_ok = all_ok and ok

    # 実際に投稿を試みたものだけ（送信失敗分も含む）を週間振り返り用ログに残す
    append_to_log(all_results, now_utc)
    return all_ok


if __name__ == '__main__':
    try:
        success = run()
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f'Error: {str(e)}')
        import traceback
        traceback.print_exc()
        sys.exit(1)
