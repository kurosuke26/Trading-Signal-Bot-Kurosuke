#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
weekly_tips.py — 週次Tips投稿バッチ（毎週日曜 08:00 JST頃に実行する想定）

collector.py / poster.py（毎日実行）とは独立した、週1回だけ動くバッチ。
「週次tips」専用チャンネルにのみ、以下4つを1つのメッセージにまとめて投稿する。

  1. 今週の市場サマリー：日経平均・TOPIXの直近1週間の騰落率と、collector.pyが
     集計したLONG/SHORT/NEUTRALのシグナル比率から、ルールベース（あらかじめ
     用意した文章テンプレートの組み合わせ）で自動生成した文章
     （TOPIXは指数そのものの取得が不安定な場合があるため、取得できなければ
     連動ETF（1306.T）にフォールバックする）
     ※ 実際のニュース記事の要約ではなく、当システムが取得できる数値だけから
     機械的に組み立てた文章である点に注意（個別の決算・金融政策・海外市況等の
     材料には一切触れていない）
  2. 直近の注目シグナル：data/latest_scan.json（直近の収集結果）に基づく
     LONG/SHORT上位銘柄の紹介
     ※ このバッチは日次スナップショットの履歴を保持していないため、正確には
     「1週間分の振り返り」ではなく「直近の収集時点」のスナップショットに
     基づく紹介である点に注意（正直な制約として明記）
  3. シグナル成績（累計）：data/trade_log.json（tracking.py）に基づく、
     LONG＋SHORT合算／LONGのみ／SHORTのみの勝率・ペイオフレシオの週次
     チェックポイント（LONGは複合スコア90点以上・上位5件、SHORTは
     PER×PBR上位10件を新規追跡対象にする設計。tracking.py参照）。あわせて、
     トレーリングストップ幅を主要な倍率で変えた場合の比較も表示する。
     ※ 2026-08-25（初回起動日、絞り込みロジック導入前）に無条件で一括
     ポジション化された1,804件は、単一日に相関した異常なコホートのため
     2026-09-15より集計から除外している（tracking.pyのLEGACY_BULK_LOAD_
     ENTRY_DATES参照）。除外後の「本来の設計」のポジションはまだ蓄積中で、
     しばらくは「集計中」表示が続く見込み
  4. 投資理論・スクリーニング手法の一言解説（週替わりで固定トピックを順番に紹介）
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import yfinance as yf

import dividend_hikes
import market_calendar
import poster
from theme_news import fetch_feed_entries
from tracking import ATR_MULTIPLIER_BY_SIGNAL, load_trade_log, compute_performance_stats

SNAPSHOT_PATH = os.getenv('SCAN_OUTPUT_PATH') or 'data/latest_scan.json'
BREAKING_LOG_PATH = os.getenv('BREAKING_ALERT_LOG_PATH') or 'data/breaking_alert_log.json'

# 【2026-09-24追加】サロン（kurosuke式資産運用キャンプ）メンバー向けに、個別株の
# 売買シグナルだけでなく「相場の大局観・広い経済ニュース」も流してほしいという要望。
# discord-ai-team側のconfig/news_sources.yamlで生存確認済みの一般経済ニュースフィードを
# 流用する（theme_news.fetch_feed_entries()を再利用、判定ロジックの二重実装はしない）。
WEEKLY_NEWS_FEEDS = [
    {'source': 'Yahoo!ニュース 経済', 'url': 'https://news.yahoo.co.jp/rss/topics/business.xml'},
]
WEEKLY_NEWS_MAX_ITEMS = 3
WEEKLY_HIKE_MAX = 6

JST = timezone(timedelta(hours=9))
SMALL_SAMPLE_THRESHOLD = 30  # この件数未満の勝率は参考程度、と注記する
WEEKDAY_JA = '月火水木金土日'

NIKKEI_TICKER = '^N225'
# TOPIX指数そのもの(998405.T)が取得できない環境向けに、連動ETF(1306.T)へ
# フォールバックする。
TOPIX_TICKERS = ['998405.T', '1306.T']

TIPS = [
    {
        'title': '📖 豆知識：日経平均とTOPIX',
        'body': '日経平均は日本の代表225社の平均、TOPIXは東証プライム全体の動きです。2つがズレた日は「一部の大型株だけが動いたのかも」と考えるヒントになります。',
    },
    {
        'title': '📖 豆知識：円安・円高',
        'body': '1ドル150円→160円が円安（円の価値が下がる）、140円が円高。円安は輸出企業に追い風で、輸入品は値上がりしやすい。円高はその逆。立場で良し悪しが変わります。',
    },
    {
        'title': '📖 豆知識：金利と株',
        'body': '金利が上がると預金や債券が魅力的になり、株から資金が移りやすくなります。企業の利息負担も増えます。ただし反応は「予想とのズレ」で決まり、毎回同じ動きにはなりません。',
    },
    {
        'title': '📖 豆知識：「割安」の目安（PER×PBR）',
        'body': 'PERは利益に対する株価の倍率、PBRは資産に対する株価の倍率。どちらも小さいほど株価は安め。このシステムは掛け算22.5以下を「割安かも」の目安にしています（安いのには理由がある場合も）。',
    },
    {
        'title': '📖 豆知識：配当利回り',
        'body': '「株価に対する1年の配当の割合」です。高く見えても、株価が下がっただけだったり、将来減配される場合も。利益の安定度とセットで見ましょう。',
    },
    {
        'title': '📖 豆知識：損切りライン',
        'body': (
            '「ここまで下がったら手放す」と買う前に決めておく価格です。決めておくと判断がブレません。'
            # 倍率はtracking.pyの本番値から埋め込む
            f'このシステムは値動きの荒さに応じて幅を自動調整します（買い候補は約{ATR_MULTIPLIER_BY_SIGNAL["LONG"]}倍分）。'
        ),
    },
    {
        'title': '📖 豆知識：ローソク足と酒田五法',
        'body': 'ローソク足は1日の始値・終値・高値・安値を1本で表したチャート。酒田五法はその並びで流れを読む江戸時代の手法です。過去2年の検証では多くが五分五分で、単独では根拠になりにくいと分かっています。',
    },
    {
        'title': '📖 豆知識：ニュースと株価',
        'body': '良いニュースでも株価が下がることは珍しくありません。株価はニュースそのものより「事前の期待とのズレ」で動くからです。見出しだけで慌てず、「みんなは何を期待していた？」と考えましょう。',
    },
    {
        'title': '📖 豆知識：分散投資',
        'body': '「卵は1つのカゴに盛るな」。銘柄・国・買う時期を分けて、1つの失敗で大きく崩れにくくする考え方です。分散しても損する場面はあります。',
    },
]


def fetch_weekly_change(ticker, period='1mo'):
    """直近5営業日（約1週間）の騰落率(%)を計算する。取得失敗・データ不足時はNone。"""
    try:
        df = yf.Ticker(ticker).history(period=period, interval='1d')
        if df is None or df.empty or 'Close' not in df.columns:
            return None
        closes = df['Close'].dropna()
        if len(closes) < 6:
            return None
        latest = float(closes.iloc[-1])
        week_ago = float(closes.iloc[-6])
        if week_ago == 0:
            return None
        return round((latest - week_ago) / week_ago * 100, 2)
    except Exception as e:
        print(f'[weekly_tips] {ticker} の週間騰落率取得に失敗: {e}')
        return None


def fetch_topix_change():
    for t in TOPIX_TICKERS:
        change = fetch_weekly_change(t)
        if change is not None:
            return change, t
    return None, None


def load_latest_snapshot():
    if not os.path.exists(SNAPSHOT_PATH):
        return None
    try:
        with open(SNAPSHOT_PATH, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def _index_trend_phrase(change):
    """指数の週間騰落率(%)を、自然な日本語の形容表現に変換する。"""
    if change is None:
        return 'データ取得失敗'
    if change >= 3:
        return f'{change:+.2f}%と大幅に上昇'
    if change >= 1:
        return f'{change:+.2f}%と上昇'
    if change >= 0.3:
        return f'{change:+.2f}%とやや上昇'
    if change > -0.3:
        return f'{change:+.2f}%とほぼ横ばい'
    if change > -1:
        return f'{change:+.2f}%とやや下落'
    if change > -3:
        return f'{change:+.2f}%と下落'
    return f'{change:+.2f}%と大幅に下落'


def _breadth_phrase(short_ratio, long_count, analyzed):
    """「割高かも」と判定された銘柄の割合から、相場全体の過熱感をひとことで表す（しきい値はposter.pyと同じ15%/30%）。"""
    if short_ratio is None or not analyzed:
        return '銘柄ごとの分析結果が取得できなかったため、相場全体の温度感は省略します。'
    if short_ratio >= 30:
        feel = 'やや過熱気味'
    elif short_ratio >= 15:
        feel = '標準的な範囲'
    else:
        feel = '過熱感は小さめ'
    return (f'「割高かも」判定は{short_ratio:.0f}%（{analyzed}銘柄中）で{feel}。'
            f'「割安かも」は{long_count}銘柄。')


def build_market_narrative_text(nikkei_change, topix_change, snapshot):
    """
    日経平均・TOPIXの週間騰落率と、直近収集時点の判定比率から、ルールベースで
    「今週の市場サマリー」を自動生成する。ニュースの要約ではなく、取得できる数値だけから
    機械的に組み立てた文章（個別の決算・金融政策・海外の動きには触れない）。
    """
    lines = [f'日経平均 {_index_trend_phrase(nikkei_change)}、TOPIX {_index_trend_phrase(topix_change)}。']

    counts = (snapshot or {}).get('counts', {})
    analyzed = counts.get('analyzed', 0)
    long_count = counts.get('LONG', 0)
    short_count = counts.get('SHORT', 0)
    short_ratio = (short_count / analyzed * 100) if analyzed else None

    lines.append(_breadth_phrase(short_ratio, long_count, analyzed))
    lines.append('※株価と機械判定から自動作成した文章です。')
    return "\n".join(lines)


def build_signal_highlight_text(snapshot):
    if not snapshot:
        return '直近の収集データが見つからないため、注目銘柄の紹介は省略します。'

    results = snapshot.get('results', {})
    full_results = [r for r in results.values() if r.get('detail') == 'full']
    long_results = sorted([r for r in full_results if r.get('signal') == 'LONG'],
                           key=lambda r: (r.get('score') if r.get('score') is not None else -1), reverse=True)
    short_results = sorted([r for r in full_results if r.get('signal') == 'SHORT'],
                            key=lambda r: (r.get('per_pbr') if r.get('per_pbr') is not None else 0), reverse=True)

    lines = []
    if long_results:
        top = long_results[0]
        score = top.get('score')
        score_text = f"{score:.0f}点" if score is not None else '判定不能'
        lines.append(f"・割安かも 1位：{top['ticker']}（{top.get('name')}）{score_text}")
    if short_results:
        top = short_results[0]
        per_pbr = top.get('per_pbr')
        if per_pbr is None:
            per_pbr_text = '不明'
        elif per_pbr > 100:
            # 利益がほぼ無い会社だと数万の値になることがあり、そのまま出すと初心者が混乱する
            per_pbr_text = '100超'
        else:
            per_pbr_text = f"{per_pbr:.1f}"
        lines.append(f"・割高かも 1位：{top['ticker']}（{top.get('name')}）割高さの目安 {per_pbr_text}")
    if not lines:
        lines.append('直近の分析では、該当する候補はありませんでした。')

    counts = snapshot.get('counts', {})
    lines.append(
        f"割安かも{counts.get('LONG', 0)}／割高かも{counts.get('SHORT', 0)}"
        f"／どちらでもない{counts.get('NEUTRAL', 0)}銘柄（直近1回の分析）"
    )
    lines.append('※機械が選んだ候補です。売買のおすすめではありません。')
    return "\n".join(lines)


def build_performance_recap_text():
    """
    このシステムの成績（累計）。「勝率」と「勝ちの大きさ÷負けの大きさ」で表し、
    専門用語は使わない。ATR倍率の比較は残している（ユーザー要望）。
    """
    trade_log = load_trade_log()
    stats = compute_performance_stats(trade_log)

    def _fmt(label, s):
        if not s or not s.get('closed_count'):
            return f'・{label}：集計中'
        parts = [f"{s['closed_count']}件", f"勝率{s['win_rate']}%"]
        if s.get('payoff_ratio') is not None:
            parts.append(f"勝ち÷負け {s['payoff_ratio']}")
        return f"・{label}：" + "・".join(parts)

    lines = [
        _fmt('割安かも候補', stats['long_only']),
        _fmt('割高かも候補', stats['short_only']),
        f"・結果待ち（保有中）：{stats['open_positions']}件",
        '（勝ち÷負けが1超なら、1回の勝ちが負けより大きい）',
    ]

    # ATR倍率の比較（LONG・SHORT共通で使っていたら、という仮定の比較。現在の本番設定
    # はLONG・SHORTで別倍率＝tracking.pyのATR_MULTIPLIER_BY_SIGNAL）
    atr_variants = stats.get('atr_variants') or {}
    if atr_variants:
        lines.append('')
        lines.append('参考：損切り幅を変えた場合（ATR倍率・仮定の比較）')
        for key in ('1.8', '2.5', '5.0'):
            lines.append(_fmt(f'ATR×{key}', atr_variants.get(key)))

    notes = []
    small = [s_ for s_ in (stats['long_only'], stats['short_only'])
             if s_ and s_.get('closed_count') and s_['closed_count'] < SMALL_SAMPLE_THRESHOLD]
    if small:
        notes.append(f'{SMALL_SAMPLE_THRESHOLD}件未満は勝率がぶれるので参考程度')
    # 初回起動日の一括ロード分は集計から除外（理由はtracking.pyのLEGACY_BULK_LOAD_ENTRY_DATES参照）
    excluded = stats.get('excluded_legacy_bulk_load_closed_count') or 0
    if excluded:
        notes.append(f'稼働初日の一括{excluded}件は除外')
    notes.append('将来の結果を約束するものではありません')
    lines.append('※' + '。'.join(notes) + '。')
    return "\n".join(lines)


def build_breaking_recap_text():
    """
    breaking_alerts.py（速報チャンネル）が今週(直近7日)投稿したアラートの短い振り返り。
    ログが無い・0件の場合はその旨を正直に返す。
    """
    if not os.path.exists(BREAKING_LOG_PATH):
        return '今週は速報の記録がありませんでした。'
    try:
        with open(BREAKING_LOG_PATH, encoding='utf-8') as f:
            log = json.load(f)
    except (json.JSONDecodeError, OSError):
        return '速報ログの読み込みに失敗したため、今回は省略します。'

    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    recent = [e for e in log if e.get('at_utc', '') >= cutoff]
    if not recent:
        return '今週は大きな動きの速報はありませんでした。'

    CATEGORY_LABELS = {
        'usdjpy_rapid': '為替急変動', 'usdjpy_daily': '為替の大幅変動',
        'nikkei_rapid': '日経急変動', 'nikkei_daily': '日経の大幅変動',
        'policy': '日銀/FRB発表', 'us_morning': '米国市場サマリー',
        'calendar': '今日の予定', 'vix': 'VIX',
        'us10y_daily': '米金利の大きな動き', 'oil_daily': '原油の大きな動き', 'gold_daily': '金の大きな動き',
        'close_summary': '大引けまとめ', 'fsa': '金融庁のお知らせ', 'dividend_hike': '増配のお知らせ',
        'btc_rapid': 'BTC急変動', 'btc_daily': 'BTCの大幅変動',
        'eth_rapid': 'ETH急変動', 'eth_daily': 'ETHの大幅変動',
        'xrp_rapid': 'XRP急変動', 'xrp_daily': 'XRPの大幅変動',
    }
    counts = {}
    for e in recent:
        label = CATEGORY_LABELS.get(e.get('category'), e.get('category', '不明'))
        counts[label] = counts.get(label, 0) + 1

    top = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:4]
    summary_line = '、'.join(f'{label}{n}' for label, n in top)
    more = '…' if len(counts) > len(top) else ''
    lines = [f'今週の速報は{len(recent)}件（{summary_line}{more}）。']

    # 動きのあった速報だけ、直近2件の見出しを添える（定例の発表・まとめは件数だけで十分）
    highlight_categories = {'usdjpy_rapid', 'usdjpy_daily', 'nikkei_rapid', 'nikkei_daily', 'vix',
                            'us10y_daily', 'oil_daily', 'gold_daily',
                            'btc_rapid', 'btc_daily', 'eth_rapid', 'eth_daily',
                            'xrp_rapid', 'xrp_daily'}
    highlights = [e for e in recent if e.get('category') in highlight_categories][-2:]
    for e in highlights:
        first_line = e.get('text', '').split('\n')[0]
        lines.append(f'・{first_line}')

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 【2026-10-28追加】「毎日の情報から、どう考えればいいか」の視点（初心者向け）
# 速報（breaking_alerts.py）のログから、今週実際に出た種類の速報にだけ
# 「これは何？／どう考える？」を付ける。ルールベース（AI生成ではない）で、
# 売買のおすすめ・将来の値動きの予想は書かず、「一般にこう言われる」という
# 見方の紹介に留める。
# ---------------------------------------------------------------------------
_CATEGORY_GROUP = {
    'usdjpy_rapid': 'usdjpy', 'usdjpy_daily': 'usdjpy',
    'nikkei_rapid': 'nikkei', 'nikkei_daily': 'nikkei',
    'policy': 'policy', 'vix': 'vix', 'us10y_daily': 'us10y',
    'oil_daily': 'oil', 'gold_daily': 'gold',
    'btc_rapid': 'crypto', 'btc_daily': 'crypto', 'eth_rapid': 'crypto',
    'eth_daily': 'crypto', 'xrp_rapid': 'crypto', 'xrp_daily': 'crypto',
    'us_morning': 'us_morning', 'calendar': 'calendar',
}
# 重要度順（先に出てきたものから、最大READING_GUIDE_MAX_ITEMS件だけ表示する）
READING_GUIDE_PRIORITY = ['policy', 'vix', 'usdjpy', 'nikkei', 'us10y', 'oil', 'gold',
                          'crypto', 'us_morning', 'calendar']
READING_GUIDE_MAX_ITEMS = 3

_CLOSE_NIKKEI_RE = re.compile(r'日経平均\s*([\d,]+)円\s*([+-]\d+(?:\.\d+)?)%')


def _recent_breaking_entries(days=7):
    """直近days日の速報ログ。読めなければ空リスト（読み解きは省略するだけ）。"""
    if not os.path.exists(BREAKING_LOG_PATH):
        return []
    try:
        with open(BREAKING_LOG_PATH, encoding='utf-8') as f:
            log = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return [e for e in log if e.get('at_utc', '') >= cutoff]


def _nikkei_daily_moves(entries):
    """大引けまとめの速報から、日ごとの日経平均の騰落率を取り出す。[(date, pct), ...]"""
    by_day = {}
    for e in sorted(entries, key=lambda x: x.get('at_utc', '')):
        if e.get('category') != 'close_summary':
            continue
        m = _CLOSE_NIKKEI_RE.search(e.get('text', ''))
        if not m:
            continue
        try:
            day = datetime.fromisoformat(e['at_utc']).astimezone(JST).date()
        except (ValueError, KeyError):
            continue
        by_day[day] = float(m.group(2))
    return sorted(by_day.items())


def _guide_for(group, latest_text):
    """速報の種類ごとの「これは何？／どう考える？」（1文）。latest_textは方向（📈/📉）の判定に使う。"""
    went_up = latest_text.lstrip().startswith('📈')
    if group == 'policy':
        return '**日銀・FRB発表**：金利の向きの知らせ。金利が上がると一般に株は重く、為替は動きやすい。「予想とのズレ」が肝心。'
    if group == 'vix':
        return '**VIX（恐怖指数）**：投資家の不安度。急に上がると値動きが荒くなりやすい。'
    if group == 'usdjpy':
        if went_up:
            return '**円安方向**：輸出企業に追い風、輸入品（ガソリン・食品）は値上がりしやすい。'
        return '**円高方向**：輸入品は安くなりやすい一方、輸出企業には逆風と言われる。'
    if group == 'nikkei':
        return '**日経の大きな動き**：「日本株全体」か「一部の大型株」か、TOPIXと比べて切り分けるのがコツ。'
    if group == 'us10y':
        return '**米国の長期金利**：世界の金利の基準。上がると成長株の重しや円安要因になりやすい。'
    if group == 'oil':
        return '**原油**：ガソリン・電気代・運送費を通じて、物価や企業の利益に響く。'
    if group == 'gold':
        return '**金**：不安なときに買われやすい「安全資産」と言われる。'
    if group == 'crypto':
        return '**暗号資産**：値動きが非常に大きく、株とは別物として見る。'
    if group == 'us_morning':
        return '**米国市場サマリー**：前夜の米国株の結果。日本株の朝の寄り付きに影響しやすい。'
    if group == 'calendar':
        return '**今日の予定**：権利付き最終日・SQ日・指標発表は、値が動きやすい日の目印。'
    return ''


def build_reading_guide_text(nikkei_change=None):
    """今週の流れ（日経平均の毎日の動き）＋今週出た速報の読み解き＋考え方の3ステップ。"""
    entries = _recent_breaking_entries()
    blocks = []

    # --- 1) 今週の流れ ---
    moves = _nikkei_daily_moves(entries)
    if len(moves) >= 2:
        strip = '／'.join(f'{d.month}/{d.day}({WEEKDAY_JA[d.weekday()]}) {pct:+.1f}%' for d, pct in moves)
        ups = sum(1 for _, p in moves if p > 0)
        downs = sum(1 for _, p in moves if p < 0)
        biggest_day, biggest = max(moves, key=lambda x: abs(x[1]))
        if abs(biggest) >= 2:
            comment = (f'{biggest_day.month}/{biggest_day.day}は{biggest:+.1f}%と大きく動いた日。'
                       '国内だけでなく海外・為替・金利が重なることが多いです。')
        elif all(abs(p) < 1 for _, p in moves):
            comment = 'どの日も1%未満。日々の上下に一喜一憂しなくてよい週です。'
        elif len(moves) >= 3 and (all(p > 0 for _, p in moves) or all(p < 0 for _, p in moves)):
            comment = '同じ向きが続いた週。背景に金利・為替・ニュースなど「理由」があることが多いです。'
        else:
            comment = '上げ下げが混ざった週。毎日より「週全体の傾き」で見ると落ち着きます。'
        blocks.append(f'**📈 今週の日経平均**（上昇{ups}日・下落{downs}日）\n{strip}\n{comment}')

    # --- 2) 今週出た速報の読み解き ---
    latest_by_group = _latest_text_by_group(entries)
    guides = [_guide_for(g, latest_by_group[g]) for g in READING_GUIDE_PRIORITY if g in latest_by_group]
    guides = [g for g in guides if g][:READING_GUIDE_MAX_ITEMS]
    if guides:
        blocks.append('**🔍 今週の速報、こう読みます**\n' + '\n'.join(f'・{g}' for g in guides))
    elif not moves:
        blocks.append('今週は大きな動きの速報がありませんでした。「何も起きない週」もふつうにあります。')

    # --- 3) 考え方の3ステップ（毎週共通） ---
    blocks.append(
        '**🧭 見るときの3ステップ**\n'
        '①何が動いた？ ②なぜ？（分からなくてOK） ③自分に関係ある？（なければ何もしないのも選択）'
    )
    return '\n\n'.join(blocks)


def fetch_weekly_news_items():
    """
    直近7日分の経済ニュース（重複タイトル除去）。公開日が取れない記事も「取得できた
    時点で新着」とみなして対象に含める（取れないことを理由に有用な見出しを落とさない）。
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).date().isoformat()
    seen_titles = set()
    items = []
    for feed in WEEKLY_NEWS_FEEDS:
        for entry in fetch_feed_entries(feed):
            title = entry.get('title')
            if not title or title in seen_titles:
                continue
            published = entry.get('published_date')
            if published and published < cutoff:
                continue
            seen_titles.add(title)
            items.append(entry)
    return items


def build_weekly_news_text(items=None):
    """
    サロンメンバー向けの「今週の経済ニュース」。個別銘柄のシグナルとは別に、
    一般的な経済ニュースの見出し・リンクをそのまま列挙する（当システムによる
    要約・論評はしない。「出典が確認できない情報は載せない」方針に沿い、
    見出し自体を一次情報として扱う）。最大WEEKLY_NEWS_MAX_ITEMS件。
    """
    if items is None:
        items = fetch_weekly_news_items()
    if not items:
        return '今週は経済ニュースの取得に失敗しました（フィード側の一時的な問題の可能性があります）。'

    lines = [f"・[{e['title']}]({e['link']})" if e.get('link') else f"・{e['title']}"
             for e in items[:WEEKLY_NEWS_MAX_ITEMS]]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 【2026-10-28追加】連想ゲーム：実際の出来事から「次に何が起きそう？」と連想をつなげて
# 遊ぶお題。予想を当てるゲームではなく「因果のつなげ方を考える練習」で、
# 答え合わせ・売買のおすすめは含めない。遊ぶ場所はコミュニケーション板。
# ---------------------------------------------------------------------------
def _latest_text_by_group(entries):
    latest = {}
    for e in sorted(entries, key=lambda x: x.get('at_utc', '')):
        g = _CATEGORY_GROUP.get(e.get('category'))
        if g:
            latest[g] = e.get('text', '')
    return latest


def _association_for(group, latest_text):
    """(お題の見出し, 連想の一例) を返す。連想の一例は一般的な因果のつながりで、予想ではない。"""
    went_up = latest_text.lstrip().startswith('📈')
    if group == 'policy':
        return ('日銀・FRBの発表', '金利が上がる → 住宅ローンは？ → 銀行の利益は？ → 円は？ → ？')
    if group == 'vix':
        return ('VIX（恐怖指数）の動き', '不安が高まる → 金・円・国債は？ → 株は？ → ？')
    if group == 'usdjpy':
        if went_up:
            return ('円安方向の動き', '円安 → ガソリンや小麦は？ → 家計は？ → 輸出企業は？ → ？')
        return ('円高方向の動き', '円高 → 輸入品・海外旅行は？ → 輸出企業は？ → 株価は？ → ？')
    if group == 'nikkei':
        if went_up:
            return ('日経平均の大きな上昇', '日経が大きく上がる → 理由は海外？為替？決算？ → 連動した業種は？ → ？')
        return ('日経平均の大きな下落', '日経が大きく下がる → 理由は？ → 下げに強かった業種は？ → なぜ？ → ？')
    if group == 'us10y':
        return ('米国の長期金利の動き', '米国の金利が動く → ドルは？ → 円は？ → 成長株は？ → ？')
    if group == 'oil':
        if went_up:
            return ('原油の上昇', '原油が上がる → ガソリン・電気代は？ → 運送費は？ → 得をする会社は？ → ？')
        return ('原油の下落', '原油が下がる → ガソリン・電気代は？ → 運送費は？ → 困る会社は？ → ？')
    if group == 'gold':
        return ('金の動き', '金が動く → なぜ金を買う？ → 株は？ → ドルは？ → ？')
    if group == 'crypto':
        return ('暗号資産の動き', '暗号資産が動く → 株やドルとの関係は？ → 規制や大企業の動きは？ → ？')
    if group == 'us_morning':
        if went_up:
            return ('米国株の上昇', '米国株が上がる → 日本株の朝は？ → 為替は？ → 注目業種は？ → ？')
        return ('米国株の下落', '米国株が下がる → 日本株の朝は？ → 為替は？ → 注目業種は？ → ？')
    if group == 'calendar':
        return ('今日の予定', '予定のある日 → どの市場が動きやすい？ → 事前に確認することは？ → ？')
    return ('', '')


def build_association_game_text(headlines=None):
    """今週の速報・ニュースから「連想ゲーム」のお題を作る。"""
    latest = _latest_text_by_group(_recent_breaking_entries())
    lines = []

    for g in READING_GUIDE_PRIORITY:
        if g in latest:
            topic, chain = _association_for(g, latest[g])
            if topic:
                lines.append(f'**🎲 お題：{topic}**\n{chain}')
                break

    if headlines:
        lines.append(f'**📰 ニュースで：**『{headlines[0]}』→ 暮らし・他社・株価にどうつながる？')

    if not lines:
        lines.append('気になったニュースを1つ選んで、「次に何が起きそう？」を連想してみましょう。')

    lines.append('正解はなし。「なぜそう思ったか」を言葉にするのが練習です。')
    lines.append('💬 **連想ゲームはコミュニケーション板でやってみましょう。**')
    lines.append('※予想・売買のおすすめではありません。')
    return '\n\n'.join(lines[:-3]) + '\n\n' + '\n'.join(lines[-3:])


def build_dividend_hike_weekly_text():
    """【2026-10-29追加】今週（直近7日）の増配のお知らせ。「割安かも」と重なる銘柄を先に、最大WEEKLY_HIKE_MAX社。"""
    today = datetime.now(JST).date()
    disclosures = dividend_hikes.fetch_disclosures(today - timedelta(days=7), today)
    if disclosures is None:
        return '今週の増配は取得できませんでした（取得元の一時的な問題の可能性があります）。'
    hikes = dividend_hikes.extract_hikes(disclosures)
    if not hikes:
        return '今週は、タイトルに「増配」と明記された開示はありませんでした。'
    long_map = dividend_hikes.load_long_candidates()
    lines, rest = dividend_hikes.build_hike_lines(hikes, long_map, WEEKLY_HIKE_MAX)
    head = f'今週は{len(hikes)}社が増配を発表しました。'
    if rest:
        lines.append(f'…ほか{rest}社')
    return head + '\n' + '\n'.join(lines) + '\n' + dividend_hikes.HIKE_EXPLAIN + '\n' + dividend_hikes.HIKE_NOTE


def build_next_week_text():
    """【2026-09-24追加・初心者向け】来週（月〜日）の休場日・SQ日・権利付き最終日・権利落ち日・
    経済イベントの一覧。土曜朝の投稿を前提に、次の月曜日から7日分（market_calendar.py）。"""
    today = datetime.now(timezone(timedelta(hours=9))).date()
    next_monday = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
    return market_calendar.build_week_text(next_monday)


def pick_weekly_tip():
    week_number = datetime.now(timezone.utc).isocalendar()[1]
    return TIPS[week_number % len(TIPS)]


EMBED_TOTAL_LIMIT = 5800  # Discordは1メッセージ内のembed文字数の合計が6000字まで（余裕を持たせる）
HEADER_CONTENT = '📅 Kurosuke割安チェッカー - 週次Tips（今週のまとめと、情報の考え方）'


def _embed_len(embed):
    return len(embed.get('title', '')) + len(embed.get('description', ''))


def split_into_payloads(embeds, content=HEADER_CONTENT):
    """embedの合計文字数が上限を超えないよう、必要なら複数メッセージに分けて投稿する。"""
    groups, cur, cur_len = [], [], 0
    for e in embeds:
        n = _embed_len(e)
        if cur and (cur_len + n > EMBED_TOTAL_LIMIT or len(cur) >= 10):
            groups.append(cur)
            cur, cur_len = [], 0
        cur.append(e)
        cur_len += n
    if cur:
        groups.append(cur)
    payloads = []
    for i, group in enumerate(groups):
        text = content if i == 0 else f'{content}（続き {i + 1}/{len(groups)}）'
        payloads.append({'content': text, 'embeds': group})
    return payloads


def build_payloads():
    tip = pick_weekly_tip()
    nikkei_change = fetch_weekly_change(NIKKEI_TICKER)
    topix_change, _ = fetch_topix_change()
    snapshot = load_latest_snapshot()
    news_items = fetch_weekly_news_items()
    embeds = [
        {'title': '📰 今週の市場サマリー', 'description': build_market_narrative_text(nikkei_change, topix_change, snapshot), 'color': 0x2ECC71},
        {'title': '🌏 今週の経済ニュース', 'description': build_weekly_news_text(news_items), 'color': 0x1ABC9C},
        {'title': '💰 今週の増配', 'description': build_dividend_hike_weekly_text(), 'color': 0xF39C12},
        {'title': '⚡ 今週あった速報', 'description': build_breaking_recap_text(), 'color': 0xE67E22},
        {'title': '🧭 今週の情報、どう考える？', 'description': build_reading_guide_text(nikkei_change), 'color': 0xE74C3C},
        {'title': '🎮 連想ゲーム',
         'description': build_association_game_text([e['title'] for e in news_items]), 'color': 0x16A085},
        {'title': '👀 機械が選んだ注目銘柄', 'description': build_signal_highlight_text(snapshot), 'color': 0x3498DB},
        {'title': '🎯 このシステムの成績', 'description': build_performance_recap_text(), 'color': 0x9B59B6},
        {'title': '🗓 来週の予定', 'description': build_next_week_text(), 'color': 0xF1C40F},
        {'title': tip['title'], 'description': tip['body'], 'color': 0x95A5A6},
    ]
    return split_into_payloads(embeds)


def post_weekly_tips():
    print("=" * 80)
    print("Kurosuke 週次Tips投稿バッチ（weekly_tips.py）")
    print("=" * 80)
    all_ok = True
    for payload in build_payloads():
        ok = poster.send_discord_message('TIPS', payload)
        all_ok = all_ok and ok
    return all_ok


if __name__ == '__main__':
    try:
        success = post_weekly_tips()
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"Error: {str(e)}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
