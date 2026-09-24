#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
breaking_alerts.py — 「速報」チャンネル向けの急変動アラート（15分おきに実行する想定）

collector.py/poster.py（1日1回、朝のシグナル投稿）とは別の、日中ずっと動く軽量バッチ。
以下10種類を「速報」チャンネル（10.は別チャンネルも可）に投稿する。

  1. 為替（USD/JPY）の急変動：直近チェック（約15分前）比、および本日(JST)の
     累計変化率がしきい値を超えたら投稿。二重通知を防ぐためクールダウンあり。
  2. 日経平均の急変動：仕組みは為替と同じ。
  3. 日銀・FRBの金融政策発表：公式RSSをポーリングし、未読の新着記事があれば投稿。
  4. 米国主要指数（NYダウ・S&P500・ナスダック）の朝の速報：JST 7時台に1日1回だけ、
     前日終値の騰落率をまとめて投稿（しきい値なし。「朝に届けばよい」という運用要望のため）。
  5. 【2026-09-24追加・初心者向け】今日の予定：JST 7時台に1日1回、休場日・SQ日・
     権利付き最終日／権利落ち日・米雇用統計/CPI/FOMC/日銀会合を1行解説付きで投稿
     （該当が無い日は投稿しない。計算はmarket_calendar.py、日程はeconomic_calendar.json）。
  6. 【2026-09-24追加・初心者向け】VIX（恐怖指数）：25・30を超えたら速報、その後20を
     下回ったら「落ち着いた」ことを1回だけ投稿。
  7. 【2026-09-24追加・初心者向け】米10年国債利回り・原油・金：朝の米国市場サマリーに
     1行解説付きで追加し、1日で大きく動いたら（利回り0.15ポイント／原油・金3%）速報。
  8. 【2026-09-24追加・初心者向け】大引け後（JST 16時台）の国内市場まとめ：日経平均・
     TOPIX・グロース250（後の2つは連動ETFで代用）。東証の営業日のみ。
  9. 【2026-09-24追加・初心者向け】金融庁の新着のうち、NISA・投資詐欺の注意喚起・
     金融経済教育など個人投資家に関係が深いものだけを投稿。
 10. 【2026-09-24追加】月末の最後の5日間、会員提出フォーム（Googleフォーム）の案内を
     1日1回（JST12時台）投稿。URLはSecretsのMEMBER_FORM_URLで渡し、未設定なら何もしない。
  ※国内CPI・GDP速報・日銀短観は、公表予定日を economic_calendar.json に登録して
    5.の「今日の予定」で知らせる（統計局・内閣府にRSSが無いため）。

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
from datetime import date, datetime, timezone, timedelta

import requests
import yfinance as yf

import market_calendar
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

# 【2026-09-24追加】日銀の新着は統計の定期公表や事務的な要領改正まで全部流れてくるため、
# 投資に関係が深いものだけに絞り、初心者向けの1行解説を付ける（上から順に最初に一致したもの）。
# FRBのフィードは元々金融政策の発表だけなので絞らない。
BOJ_TOPICS = [
    ('動画', None),  # 「記者会見の動画の配信開始」は会見本体と重複するので流さない
    ('当面の金融政策運営', '日本の金利の方針が決まりました。円相場・銀行株・不動産株などに影響しやすい発表です'),
    ('金融政策決定会合', '日本の金利の方針を決める会議に関する発表です'),
    ('展望レポート', '日銀が今後の景気と物価の見通しを示すレポート。金利の先行きを占う材料になります'),
    ('経済・物価情勢の展望', '日銀が今後の景気と物価の見通しを示すレポート。金利の先行きを占う材料になります'),
    ('短観', '日銀が約1万社に景気の実感を聞く調査。「業況判断」がプラスなら景気が良いと感じる会社が多い'),
    ('記者会見', '日銀の総裁・副総裁が今後の方針を説明する場。発言ひとつで円相場や株価が動くことがあります'),
    ('主な意見', '会合で出た委員の意見の要約。次の利上げ・利下げの手がかりになります'),
    ('議事要旨', '過去の会合の議論の記録。次の利上げ・利下げの手がかりになります'),
]


def _boj_topic(title):
    """(流すか, 解説)。どのトピックにも当たらなければ流さない。"""
    for keyword, explain in BOJ_TOPICS:
        if keyword in title:
            return explain is not None, explain
    return False, None


MAX_SEEN_LINKS = 200  # 状態ファイル肥大化防止（各フィードごとの既読リンク保持上限）

US_INDEX_TICKERS = [
    ('^DJI', 'NYダウ'),
    ('^GSPC', 'S&P500'),
    ('^IXIC', 'ナスダック'),
]

# 【2026-09-24追加】VIX（恐怖指数）。水準がこれらを上回ったら速報し、落ち着き水準を
# 下回ったら「落ち着いた」ことを1回だけ知らせる
VIX_TICKER = '^VIX'
VIX_WARN_LEVEL = env_float('BREAKING_VIX_WARN_LEVEL', 25)
VIX_HIGH_LEVEL = env_float('BREAKING_VIX_HIGH_LEVEL', 30)
VIX_CALM_LEVEL = env_float('BREAKING_VIX_CALM_LEVEL', 20)
# 登録済みの経済イベント日程の残りがこの日数を切ったら、ログで追記を促す
CALENDAR_MIN_DAYS_LEFT = 30

# 【2026-09-24追加・初心者向け】金利・商品。朝の米国市場サマリーに並べ、1日で大きく
# 動いたら速報する（本日(JST)最初のチェック時点との比較）。unit='pt'は利回りの差（%ポイント）
MACRO_INSTRUMENTS = [
    {'key': 'us10y', 'ticker': '^TNX', 'label': '米10年国債利回り', 'unit': 'pt',
     'threshold': env_float('BREAKING_US10Y_DAILY_THRESHOLD_PT', 0.15),
     'explain': '世界の金利の基準。上がると株、特に成長株には逆風になりやすい'},
    {'key': 'oil', 'ticker': 'CL=F', 'label': '原油（WTI）', 'unit': 'pct',
     'threshold': env_float('BREAKING_OIL_DAILY_THRESHOLD_PCT', 3.0),
     'explain': 'ガソリンや電気代など物価に直結。上がると物価高・インフレの心配が強まる'},
    {'key': 'gold', 'ticker': 'GC=F', 'label': '金（ゴールド）', 'unit': 'pct',
     'threshold': env_float('BREAKING_GOLD_DAILY_THRESHOLD_PCT', 3.0),
     'explain': '「守りの資産」。世の中が不安になると買われやすい'},
]
MACRO_COOLDOWN_MINUTES = env_float('BREAKING_MACRO_COOLDOWN_MINUTES', 240)

# 大引け後の国内市場まとめ（JST 16時台、東証の営業日のみ）。TOPIX・グロース250の指数そのものは
# yfinanceで取れないため、連動ETFの騰落率で代用する（表示でもその旨を明記）
CLOSE_SUMMARY_TICKERS = [
    ('^N225', '日経平均', '日本を代表する225社の平均'),
    ('1306.T', 'TOPIX（連動ETFで代用）', '東証プライムのほぼ全銘柄＝日本株全体の動き'),
    ('2516.T', 'グロース250（連動ETFで代用）', '新興・成長企業の動き。値動きが大きめ'),
]

# 【2026-09-24追加】月末の最後のN日間、会員提出フォーム（Googleフォーム）の案内を1日1回流す。
# URLはGitHub Secrets（MEMBER_FORM_URL）で渡す（公開リポジトリのため、ログや状態ファイルに
# URLを書かない）。未設定なら何もしない。投稿先は MEMBER_FORM_WEBHOOK_URL があればそこ、
# 無ければ「速報」チャンネル。
MEMBER_FORM_URL = os.getenv('MEMBER_FORM_URL', '').strip()
MEMBER_FORM_WEBHOOK_URL = os.getenv('MEMBER_FORM_WEBHOOK_URL', '').strip()
MEMBER_FORM_TITLE = os.getenv('MEMBER_FORM_TITLE', '').strip() or '会員提出フォーム'
MEMBER_FORM_LAST_DAYS = int(env_float('MEMBER_FORM_LAST_DAYS', 5))
MEMBER_FORM_POST_HOUR = int(env_float('MEMBER_FORM_POST_HOUR', 12))  # JST。朝7時台の投稿と重ならない昼に
# 投稿者として表示する名前（速報の「Kurosuke速報」とは分ける）
MEMBER_FORM_USERNAME = os.getenv('MEMBER_FORM_USERNAME', '').strip() or '【フォーム入力のリマインド】'

# 金融庁の新着情報のうち、個人投資家に関係が深いものだけを投稿する
FSA_FEED = {'source': '金融庁', 'url': 'https://www.fsa.go.jp/fsaNewsListAll_rss2.xml'}
FSA_KEYWORDS = ['NISA', 'つみたて', 'iDeCo', 'イデコ', '金融経済教育', 'J-FLEC', '詐欺',
                '注意喚起', '無登録', '税制', '貯蓄から投資', '資産形成']
FSA_EXCLUDE_KEYWORDS = ['届出一覧', '人事異動']  # 定期的な事務更新は流さない


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
            text = f'📜 **{source}発表**：{entry["title"]}\n{entry["link"]}'
            if source == '日銀':
                wanted, explain = _boj_topic(entry['title'] or '')
                if not wanted:
                    continue
                text += f'\n💡 {explain}'
            results.append(('policy', text))

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

    # 【2026-09-24追加】金利・原油・金（1行解説付き）
    for inst in MACRO_INSTRUMENTS:
        pair = _prev_and_latest_close(inst['ticker'])
        if pair is None:
            lines.append(f'{inst["label"]}：データ取得不可')
            continue
        lines.append(f'{_format_change(inst, *pair)}\n　💡 {inst["explain"]}')

    state['us_morning_report_date_jst'] = today_jst_str
    if not lines:
        return []
    return [('us_morning', '🌅 **米国市場サマリー（前日終値比）**\n' + '\n'.join(lines))]


def _prev_and_latest_close(ticker, period='5d'):
    """日足の(前日終値, 最新終値, 最新日付)。取れなければNone。"""
    try:
        closes = yf.Ticker(ticker).history(period=period, interval='1d')['Close'].dropna()
        if len(closes) < 2:
            return None
        return float(closes.iloc[-2]), float(closes.iloc[-1]), closes.index[-1].date()
    except Exception as e:
        print(f'[breaking_alerts] {ticker} の日足取得に失敗: {e}', file=sys.stderr)
        return None


def _format_change(inst, prev, latest, _latest_date=None):
    if inst['unit'] == 'pt':
        diff = latest - prev
        arrow = '📈' if diff >= 0 else '📉'
        return f'{arrow} {inst["label"]} {latest:.2f}%（{diff:+.2f}ポイント）'
    change_pct = (latest - prev) / prev * 100
    arrow = '📈' if change_pct >= 0 else '📉'
    return f'{arrow} {inst["label"]} {change_pct:+.2f}%'


# ---------------------------------------------------------------------------
# 7. 金利・原油・金が1日で大きく動いた時の速報
# ---------------------------------------------------------------------------
def check_macro_moves(state, now_utc, today_jst_str):
    results = []
    for inst in MACRO_INSTRUMENTS:
        price = fetch_last_price(inst['ticker'])
        if price is None:
            continue
        s = state.setdefault(inst['key'], {})
        if s.get('day_start_date_jst') != today_jst_str:
            s['day_start_date_jst'] = today_jst_str
            s['day_start_price'] = price
        start = s.get('day_start_price')

        in_cooldown = False
        if s.get('last_alert_at_utc'):
            try:
                elapsed = (now_utc - datetime.fromisoformat(s['last_alert_at_utc'])).total_seconds() / 60
                in_cooldown = elapsed < MACRO_COOLDOWN_MINUTES
            except ValueError:
                pass

        if start and not in_cooldown:
            if inst['unit'] == 'pt':
                move, shown = price - start, f'{start:.2f}% → {price:.2f}%（{price - start:+.2f}ポイント）'
            else:
                move = (price - start) / start * 100
                shown = f'{start:,.2f} → {price:,.2f}（{move:+.2f}%）'
            if abs(move) >= inst['threshold']:
                arrow = '📈' if move > 0 else '📉'
                results.append((f'{inst["key"]}_daily',
                    f'{arrow} **{inst["label"]}が大きく動いています**：{shown}、本日累計\n💡 {inst["explain"]}'))
                s['last_alert_at_utc'] = now_utc.isoformat()
        s['last_price'] = price
    return results


# ---------------------------------------------------------------------------
# 8. 大引け後の国内市場まとめ（JST 16時台、東証の営業日に1回）
# ---------------------------------------------------------------------------
def check_close_summary(state, now_jst, today_jst_str):
    if now_jst.hour != 16 or state.get('close_summary_date_jst') == today_jst_str:
        return []
    today = now_jst.date()
    if not market_calendar.is_market_open(today):
        return []

    lines = []
    for ticker, label, explain in CLOSE_SUMMARY_TICKERS:
        pair = _prev_and_latest_close(ticker)
        if pair is None:
            continue
        prev, latest, latest_date = pair
        if latest_date != today:
            continue  # まだ本日の終値が反映されていない（次の回で再試行）
        change_pct = (latest - prev) / prev * 100
        arrow = '📈' if change_pct >= 0 else '📉'
        value = f'{latest:,.0f}円 ' if ticker == '^N225' else ''
        lines.append(f'{arrow} {label} {value}{change_pct:+.2f}%\n　💡 {explain}')

    if len(lines) < len(CLOSE_SUMMARY_TICKERS):
        # 一部しかそろっていない時は16時台の次の回を待つ。16:45の回（最後）なら取れた分で出す
        if now_jst.minute < 45 or not lines:
            return []
    state['close_summary_date_jst'] = today_jst_str

    note = '※TOPIX・グロース250は指数そのものが取れないため、連動するETFの値動きで代用しています'
    return [('close_summary', '🔔 **今日の東京市場（大引け）**\n' + '\n'.join(lines) + '\n' + note)]


# ---------------------------------------------------------------------------
# 10. 月末の会員提出フォームの案内（最後のN日間、1日1回）
# ---------------------------------------------------------------------------
def build_member_form_message(today):
    """月末の最後のN日間なら案内文、それ以外はNone。"""
    if not MEMBER_FORM_URL:
        return None
    month_end = (date(today.year + (today.month == 12), today.month % 12 + 1, 1) - timedelta(days=1))
    days_left = (month_end - today).days
    if days_left >= MEMBER_FORM_LAST_DAYS:
        return None
    when = '今日が締切です！' if days_left == 0 else f'締切まであと{days_left}日'
    return (f'📝 **{today.month}月の{MEMBER_FORM_TITLE}**（締切：{month_end.month}/{month_end.day}、{when}）\n'
            f'まだの方は、月末までに提出をお願いします。\n{MEMBER_FORM_URL}')


def post_member_form_if_due(state, now_jst, today_jst_str):
    """投稿先が速報チャンネルと違うことがあるため、他の速報とは別に直接送る。"""
    if now_jst.hour != MEMBER_FORM_POST_HOUR or state.get('member_form_post_date_jst') == today_jst_str:
        return True
    text = build_member_form_message(now_jst.date())
    if text is None:
        return True
    state['member_form_post_date_jst'] = today_jst_str
    payload = {'content': text, 'username': MEMBER_FORM_USERNAME}
    print('[breaking_alerts] 会員提出フォームの案内を投稿します')  # URLはログに出さない
    if not MEMBER_FORM_WEBHOOK_URL:
        return poster.send_discord_message('BREAKING', payload)
    try:
        response = requests.post(MEMBER_FORM_WEBHOOK_URL, json=payload, timeout=30)
        response.raise_for_status()
        return True
    except Exception as e:
        print(f'[breaking_alerts] 会員提出フォームの投稿に失敗: {type(e).__name__}', file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# 9. 金融庁の新着（NISA・投資詐欺の注意喚起など、個人投資家に関係が深いものだけ）
# ---------------------------------------------------------------------------
def check_fsa_feed(state):
    is_first_run = 'fsa_seen_links' not in state
    seen = set(state.get('fsa_seen_links', []))
    results = []
    for entry in fetch_feed_entries(FSA_FEED):
        link = entry['link'] or entry['title']
        if not link or link in seen:
            continue
        seen.add(link)
        title = entry['title'] or ''
        if is_first_run:
            continue  # 初回は既読登録のみ（過去分を一斉に流さない）
        if any(k in title for k in FSA_EXCLUDE_KEYWORDS):
            continue
        if any(k in title for k in FSA_KEYWORDS):
            results.append(('fsa', f'🏛 **金融庁のお知らせ**：{title}\n{entry["link"]}'))
    state['fsa_seen_links'] = list(seen)[-MAX_SEEN_LINKS:]
    return results


# ---------------------------------------------------------------------------
# 5. 今日の予定（JST 7時台に1日1回。休場日・SQ日・権利付き最終日・経済イベント）
# ---------------------------------------------------------------------------
def check_morning_calendar(state, now_jst, today_jst_str):
    if now_jst.hour != 7:
        return []
    if state.get('calendar_post_date_jst') == today_jst_str:
        return []
    state['calendar_post_date_jst'] = today_jst_str

    today = now_jst.date()
    calendar = market_calendar.load_economic_calendar()
    days_left = market_calendar.days_of_calendar_left(today, calendar)
    if days_left < CALENDAR_MIN_DAYS_LEFT:
        print(f'[breaking_alerts] 警告: economic_calendar.json の登録済み日程が残り{days_left}日です。'
              '公式サイトから翌年分を追記してください', file=sys.stderr)

    items = market_calendar.build_morning_items(today, calendar)
    if not items:
        return []
    return [('calendar', market_calendar.format_morning_message(today, items))]


# ---------------------------------------------------------------------------
# 6. VIX（恐怖指数）の水準チェック
# ---------------------------------------------------------------------------
def check_vix(state, now_utc):
    value = fetch_last_price(VIX_TICKER)
    if value is None:
        return []

    vix = state.get('vix')
    if vix is None:
        # 初回は現在の水準を記録するだけ（既に高い状態でも、いきなり速報しない）
        state['vix'] = {'last': value, 'alerted_level': 0, 'last_check_at_utc': now_utc.isoformat()}
        return []

    results = []
    explain = '💡 VIXは「米国株の恐怖指数」。投資家が先行きを不安に思うほど上がります（普段は10〜20程度）'
    alerted = vix.get('alerted_level', 0)
    level = 2 if value >= VIX_HIGH_LEVEL else 1 if value >= VIX_WARN_LEVEL else 0
    if level > alerted:
        if level == 2:
            text = (f'😨 **VIXが{VIX_HIGH_LEVEL:g}を超えました**：{value:.1f}\n'
                    f'市場がかなり不安定な状態です。慌てて売らず、値動きが落ち着くのを待つのも選択肢です\n{explain}')
        else:
            text = (f'⚠️ **VIXが{VIX_WARN_LEVEL:g}を超えました**：{value:.1f}\n'
                    f'市場の不安が高まり、株価が大きく動きやすくなっています\n{explain}')
        results.append(('vix', text))
        vix['alerted_level'] = level
    elif alerted and value < VIX_CALM_LEVEL:
        results.append(('vix', f'😌 **VIXが{VIX_CALM_LEVEL:g}を下回りました**：{value:.1f}\n'
                               f'市場の不安はひとまず落ち着いてきています\n{explain}'))
        vix['alerted_level'] = 0

    vix['last'] = value
    vix['last_check_at_utc'] = now_utc.isoformat()
    return results


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
    all_results += check_vix(state, now_utc)
    all_results += check_macro_moves(state, now_utc, today_jst_str)
    all_results += check_policy_feeds(state)
    all_results += check_fsa_feed(state)
    all_results += check_close_summary(state, now_jst, today_jst_str)
    all_results += check_morning_calendar(state, now_jst, today_jst_str)
    all_results += check_us_morning_report(state, now_jst, today_jst_str)
    form_ok = post_member_form_if_due(state, now_jst, today_jst_str)

    save_state(state)

    if not all_results:
        print('[breaking_alerts] 今回は速報対象なしでした')
        return form_ok

    all_ok = True
    for category, text in all_results:
        print(f'[breaking_alerts] 投稿({category}): {text[:60]}...')
        ok = poster.send_discord_message('BREAKING', build_payload(text))
        all_ok = all_ok and ok

    # 実際に投稿を試みたものだけ（送信失敗分も含む）を週間振り返り用ログに残す
    append_to_log(all_results, now_utc)
    return all_ok and form_ok


def check_member_form_settings():
    """会員提出フォームの設定だけを確かめる（投稿しない。URLやWebhookの値はログに出さない）。
    Webhookは読み取り専用のGETで有効性とチャンネルを確認する（Discordは投稿されない）。"""
    ok = True
    if not MEMBER_FORM_URL:
        print('❌ MEMBER_FORM_URL が未設定です（GoogleフォームのURLを登録してください）')
        ok = False
    elif MEMBER_FORM_URL.startswith(('https://forms.gle/', 'https://docs.google.com/forms/')):
        print('✅ MEMBER_FORM_URL：GoogleフォームのURLの形式です')
    elif MEMBER_FORM_URL.startswith('https://discord.com/api/webhooks/'):
        print('❌ MEMBER_FORM_URL にDiscordのWebhook URLが入っています（GoogleフォームのURLを入れてください）')
        ok = False
    else:
        print('⚠️ MEMBER_FORM_URL：GoogleフォームのURLに見えません（https://forms.gle/… か https://docs.google.com/forms/… のはず）')
        ok = False

    if not MEMBER_FORM_WEBHOOK_URL:
        print('ℹ️ MEMBER_FORM_WEBHOOK_URL は未設定です（案内は「速報」チャンネルに流れます）')
    elif not MEMBER_FORM_WEBHOOK_URL.startswith(('https://discord.com/api/webhooks/',
                                                  'https://discordapp.com/api/webhooks/')):
        kind = 'GoogleフォームのURL' if 'google' in MEMBER_FORM_WEBHOOK_URL or 'forms.gle' in MEMBER_FORM_WEBHOOK_URL else 'Webhook以外の値'
        print(f'❌ MEMBER_FORM_WEBHOOK_URL に{kind}が入っています（DiscordのWebhook URLを入れてください）')
        ok = False
    else:
        try:
            r = requests.get(MEMBER_FORM_WEBHOOK_URL, timeout=15)
            if r.status_code == 200:
                name = r.json().get('name', '（名前なし）')
                print(f'✅ MEMBER_FORM_WEBHOOK_URL：有効なWebhookです（Webhook名「{name}」）。案内はこのWebhookのチャンネルに流れます')
            else:
                print(f'❌ MEMBER_FORM_WEBHOOK_URL：Discordが受け付けませんでした（HTTP {r.status_code}）。削除済みか、コピーが途中で切れている可能性があります')
                ok = False
        except Exception as e:
            print(f'❌ MEMBER_FORM_WEBHOOK_URL：確認中にエラー（{type(e).__name__}）')
            ok = False

    today = datetime.now(JST).date()
    month_end = date(today.year + (today.month == 12), today.month % 12 + 1, 1) - timedelta(days=1)
    first_day = month_end - timedelta(days=MEMBER_FORM_LAST_DAYS - 1)
    print(f'ℹ️ 今月の案内：{first_day.month}/{first_day.day}〜{month_end.month}/{month_end.day} の毎日{MEMBER_FORM_POST_HOUR}時台（JST）')
    return ok


if __name__ == '__main__':
    if '--check-member-form' in sys.argv:
        sys.exit(0 if check_member_form_settings() else 1)
    try:
        success = run()
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f'Error: {str(e)}')
        import traceback
        traceback.print_exc()
        sys.exit(1)
