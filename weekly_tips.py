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
WEEKLY_NEWS_MAX_ITEMS = 5

JST = timezone(timedelta(hours=9))
SMALL_SAMPLE_THRESHOLD = 30  # この件数未満の勝率は参考程度、と注記する
WEEKDAY_JA = '月火水木金土日'

NIKKEI_TICKER = '^N225'
# TOPIX指数そのもの(998405.T)が取得できない環境向けに、連動ETF(1306.T)へ
# フォールバックする。
TOPIX_TICKERS = ['998405.T', '1306.T']

TIPS = [
    {
        'title': '📖 豆知識：日経平均とTOPIXは何が違う？',
        'body': (
            '日経平均は「日本を代表する225社の株価の平均」、TOPIXは「東証プライム市場のほぼ全社をまとめた'
            '指数」です。日経平均は値段の高い一部の大型株に引っ張られやすく、TOPIXは日本株全体の体温に'
            '近い、と言われます。2つの動きがズレた日は「一部の銘柄だけが動いたのかも」と考えるヒントになります。'
        ),
    },
    {
        'title': '📖 豆知識：円安・円高って結局どういうこと？',
        'body': (
            '1ドル＝150円が160円になるのが「円安」（円の価値が下がる）、140円になるのが「円高」です。'
            '円安は、海外で稼ぐ輸出企業には追い風になりやすい一方、輸入するガソリンや食料品は値上がりしやすい。'
            '円高はその逆。良い悪いは立場で変わるので、「自分の生活・持っている株にどう関係するか」で見るのがコツです。'
        ),
    },
    {
        'title': '📖 豆知識：金利が上がると株が下がりやすい、と言われる理由',
        'body': (
            '金利が上がると、預金や債券でも安全にお金が増えるので、リスクのある株から資金が移りやすくなります。'
            'また、会社が借りるお金の利息も増えて利益が減りやすくなります。だから日銀・FRB（米国の中央銀行）の'
            '発表は注目されます。ただし「予想より高いか低いか」で反応が変わるので、いつも同じ動きになるとは限りません。'
        ),
    },
    {
        'title': '📖 豆知識：「割安」とは？（PER×PBRの見方）',
        'body': (
            'PERは「会社の利益に対して株価が何倍か」、PBRは「会社の持つ資産に対して株価が何倍か」。'
            'どちらも小さいほど「株価が安め」の目安です。このシステムは2つを掛け算して22.5以下を'
            '「割安かも」の目安にしています。ただし安いのには理由がある場合もあるので、'
            '「候補を見つける道具」であって「買えば儲かる印」ではありません。'
        ),
    },
    {
        'title': '📖 豆知識：配当利回りは高ければ良い？',
        'body': (
            '配当利回りは「株価に対して1年にもらえる配当の割合」です。高いほど魅力的に見えますが、'
            '株価が大きく下がった結果、利回りだけ高く見えている場合や、将来配当が減る（減配）可能性もあります。'
            '利回りだけでなく「利益が安定しているか」「配当を続けられる体力があるか」もセットで見るのが基本です。'
        ),
    },
    {
        'title': '📖 豆知識：損切りラインとは？',
        'body': (
            '損切りラインは「ここまで下がったら手放す」と、買う前に決めておく価格のことです。'
            '決めておかないと、下がったときに「もう少し待てば戻るかも」と判断がブレやすくなります。'
            # 倍率はtracking.pyの本番値から埋め込む
            f'このシステムは値動きの荒さに合わせて自動で幅を変えています（買い候補は値動きの約{ATR_MULTIPLIER_BY_SIGNAL["LONG"]}倍分が目安）。'
            '位置は人それぞれ。大事なのは「事前に決めること」です。'
        ),
    },
    {
        'title': '📖 豆知識：ローソク足と酒田五法をざっくり',
        'body': (
            'ローソク足は「その日の始値・終値・高値・安値」を1本の足で表したチャートです。酒田五法は江戸時代の'
            '米相場から伝わる、ローソク足の並びで流れを読む方法。このシステムで過去約2年を検証した結果、'
            '多くのパターンは当たり外れが五分五分に近く、単独で売買を決める根拠にはなりにくいと分かっています。'
            '「他の材料の補強」くらいに使うのが現実的です。'
        ),
    },
    {
        'title': '📖 豆知識：ニュースの見出しと株価は、必ずしも同じ向きに動かない',
        'body': (
            '良いニュースが出ても株価が下がる、悪いニュースで上がる、ということは珍しくありません。'
            '株価はニュースそのものより「事前の期待とのズレ」で動くことが多いからです。'
            '見出しだけを見て慌てて動くより、「みんなは何を期待していたか」を一呼吸おいて考えるのが近道です。'
        ),
    },
    {
        'title': '📖 豆知識：分散投資って何をどう分けるの？',
        'body': (
            '「卵は1つのカゴに盛るな」という格言のとおり、1つの銘柄・1つの国・1つの時期に全部を'
            '集中させないことです。銘柄を分ける、国内と海外に分ける、一括でなく時期をずらして買う、などがあります。'
            '分散しても損をする場面はありますが、「1つの失敗で大きく崩れにくくする」ための考え方です。'
        ),
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
    """
    「割高かも」と判定された銘柄の割合から、相場全体の過熱感をひとことで表す。
    しきい値（15%/30%）はposter.py側の市場警戒ロジックと揃えている。
    """
    if short_ratio is None or not analyzed:
        return '銘柄ごとの分析結果が取得できなかったため、相場全体の温度感は省略します。'
    if short_ratio >= 30:
        mood = (f'機械で分析した{analyzed}銘柄のうち{short_ratio:.1f}%が「今は割高かも」と判定されました。'
                '株価が上がりすぎている銘柄が多く、やや過熱気味の1週間でした。')
    elif short_ratio >= 15:
        mood = (f'機械で分析した{analyzed}銘柄のうち{short_ratio:.1f}%が「今は割高かも」と判定されました。'
                '過熱感も割安感も、ふつうの範囲の1週間でした。')
    else:
        mood = (f'機械で分析した{analyzed}銘柄のうち、「今は割高かも」と判定されたのは{short_ratio:.1f}%だけでした。'
                '過熱感は小さめの1週間でした。')
    mood += f'一方、「割安かも」と判定された銘柄は{long_count}銘柄でした。'
    return mood


def build_market_narrative_text(nikkei_change, topix_change, snapshot):
    """
    日経平均・TOPIXの週間騰落率と、直近収集時点の判定比率から、ルールベースで
    「今週の市場サマリー」文章を自動生成する。

    【正直な注意点】
    これは実際のニュース記事の要約ではない。当システムが取得できる数値
    （指数騰落率・自前の判定比率）だけから、あらかじめ用意した文章テンプレートを
    機械的に組み合わせた文章であり、個別の決算・金融政策・海外市況などの
    材料には一切触れていない。
    """
    lines = [
        f'日経平均は{_index_trend_phrase(nikkei_change)}、'
        f'TOPIXは{_index_trend_phrase(topix_change)}という1週間でした。',
        '（日経平均＝日本を代表する225社の株価の平均／TOPIX＝東証プライム市場のほぼ全社の動き）',
    ]

    counts = (snapshot or {}).get('counts', {})
    analyzed = counts.get('analyzed', 0)
    long_count = counts.get('LONG', 0)
    short_count = counts.get('SHORT', 0)
    short_ratio = (short_count / analyzed * 100) if analyzed else None

    lines.append(_breadth_phrase(short_ratio, long_count, analyzed))
    lines.append(
        '※この文章はニュースの要約ではなく、株価の動きと機械的な判定の割合から自動で作っています'
        '（個別の決算・金融政策・海外の動きは反映していません）。'
    )
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
        score_text = f"{score:.0f}点（100点満点）" if score is not None else '判定不能'
        lines.append(f"・「割安かも」の1位候補：{top['ticker']}（{top.get('name')}）総合スコア {score_text}")
    if short_results:
        top = short_results[0]
        per_pbr = top.get('per_pbr')
        if per_pbr is None:
            per_pbr_text = '不明'
        elif per_pbr > 100:
            # 利益がほぼ無い会社だと数万の値になることがあり、そのまま出すと初心者が混乱する
            per_pbr_text = '100超（利益が少なく、株価が極端に割高に見える状態）'
        else:
            per_pbr_text = f"{per_pbr:.1f}（数字が大きいほど割高な傾向）"
        lines.append(
            f"・「割高かも」で注意したい1位：{top['ticker']}（{top.get('name')}）"
            f"割高さの目安（PER×PBR）{per_pbr_text}"
        )
    if not lines:
        lines.append('直近の分析では、該当する候補はありませんでした。')

    counts = snapshot.get('counts', {})
    lines.append(
        f"直近の分析結果：割安かも {counts.get('LONG', 0)}銘柄／割高かも {counts.get('SHORT', 0)}銘柄"
        f"／どちらでもない {counts.get('NEUTRAL', 0)}銘柄"
    )
    lines.append(
        '※機械が数字だけで選んだ「候補」です。売買をすすめるものではありません。'
        '直近1回の分析時点の結果で、1週間分の集計ではありません。'
    )
    return "\n".join(lines)


def build_performance_recap_text():
    """
    このシステムの成績（累計）。専門用語（ペイオフレシオ等）は使わず、
    「勝率」と「勝ったときの利益÷負けたときの損失」を平易な言葉で説明する。
    ATR倍率の比較表は初心者には難しすぎるため、週次の投稿からは外している
    （数字はtracking.py・compute_performance_stats()側にはそのまま残っている）。
    """
    trade_log = load_trade_log()
    stats = compute_performance_stats(trade_log)

    def _fmt(label, s):
        if not s or not s.get('closed_count'):
            return f'・{label}：まだ結果が出ていません（集計中）'
        parts = [f"結果が出たのは{s['closed_count']}件", f"勝率{s['win_rate']}%"]
        if s.get('payoff_ratio') is not None:
            parts.append(f"勝ちの大きさ÷負けの大きさ＝{s['payoff_ratio']}")
        return f"・{label}：" + "、".join(parts)

    lines = [
        _fmt('割安かも候補（買い方向）', stats['long_only']),
        _fmt('割高かも候補（売り方向）', stats['short_only']),
        f"・まだ結果が出ていない（保有中）：{stats['open_positions']}件",
        '',
        '📘 見方：勝率は「利益で終わった割合」。「勝ちの大きさ÷負けの大きさ」が1を超えると、'
        '1回あたりの勝ちが負けより大きいという意味です（勝率が低くても、これが大きければ全体でプラスになりえます）。',
    ]
    # 件数が少ないうちは、勝率が1件の結果で大きく変わる
    small = [s_ for s_ in (stats['long_only'], stats['short_only'])
             if s_ and s_.get('closed_count') and s_['closed_count'] < SMALL_SAMPLE_THRESHOLD]
    if small:
        lines.append(
            f"※結果が出た件数がまだ少ない（{SMALL_SAMPLE_THRESHOLD}件未満）ものは、勝率が大きくぶれます。"
            "数字は参考程度に見てください。"
        )
    # 【2026-09-15追加】初回起動日の一括ロード分（絞り込みロジック導入前の
    # 異常なコホート）を集計から除外している旨を明記する。理由の詳細は
    # tracking.pyのLEGACY_BULK_LOAD_ENTRY_DATESコメント参照。
    excluded = stats.get('excluded_legacy_bulk_load_closed_count') or 0
    if excluded:
        lines.append(
            f"※システム稼働初日に一括で記録した{excluded}件は、特殊な偏りがあるため集計から外しています。"
        )
    lines.append('※過去の成績は将来の結果を約束するものではありません。')
    return "\n".join(lines)


def build_breaking_recap_text():
    """
    breaking_alerts.py（速報チャンネル）が今週(直近7日)投稿したアラートを、
    短文の振り返りとしてまとめる。ログが無い・0件の場合はその旨を正直に返す。
    """
    if not os.path.exists(BREAKING_LOG_PATH):
        return '今週は速報の記録がありませんでした（breaking_alerts.py未実行の可能性）。'
    try:
        with open(BREAKING_LOG_PATH, encoding='utf-8') as f:
            log = json.load(f)
    except (json.JSONDecodeError, OSError):
        return '速報ログの読み込みに失敗したため、今回は振り返りを省略します。'

    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    recent = [e for e in log if e.get('at_utc', '') >= cutoff]
    if not recent:
        return '今週は為替・日経平均の急変動や日銀/FRB発表の速報はありませんでした。'

    CATEGORY_LABELS = {
        'usdjpy_rapid': '為替急変動', 'usdjpy_daily': '為替（本日大幅変動）',
        'nikkei_rapid': '日経急変動', 'nikkei_daily': '日経（本日大幅変動）',
        'policy': '日銀/FRB発表', 'us_morning': '米国市場サマリー',
        'calendar': '今日の予定', 'vix': 'VIX（恐怖指数）',
        'us10y_daily': '米金利の大きな動き', 'oil_daily': '原油の大きな動き', 'gold_daily': '金の大きな動き',
        'close_summary': '大引けまとめ', 'fsa': '金融庁のお知らせ',
        'btc_rapid': 'BTC急変動', 'btc_daily': 'BTC（本日大幅変動）',
        'eth_rapid': 'ETH急変動', 'eth_daily': 'ETH（本日大幅変動）',
        'xrp_rapid': 'XRP急変動', 'xrp_daily': 'XRP（本日大幅変動）',
    }
    counts = {}
    for e in recent:
        label = CATEGORY_LABELS.get(e.get('category'), e.get('category', '不明'))
        counts[label] = counts.get(label, 0) + 1

    summary_line = '、'.join(f'{label}{n}件' for label, n in counts.items())
    lines = [f'今週の速報は合計{len(recent)}件（{summary_line}）でした。']

    # 為替・日経の急変動系とVIXだけ、直近3件の見出しを短く添える（政策発表・朝の市場サマリーは
    # 件数が把握できれば十分なため、本文の再掲はしない）
    highlight_categories = {'usdjpy_rapid', 'usdjpy_daily', 'nikkei_rapid', 'nikkei_daily', 'vix',
                            'us10y_daily', 'oil_daily', 'gold_daily',
                            'btc_rapid', 'btc_daily', 'eth_rapid', 'eth_daily',
                            'xrp_rapid', 'xrp_daily'}
    highlights = [e for e in recent if e.get('category') in highlight_categories][-3:]
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
READING_GUIDE_MAX_ITEMS = 4

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
    """速報の種類ごとの「これは何？／どう考える？」。latest_textは方向（📈/📉）の判定に使う。"""
    went_up = latest_text.lstrip().startswith('📈')
    if group == 'policy':
        return ('**日銀・FRB（米国の中央銀行）の発表**：金利の向きを知らせるものです。金利が上がると一般に株には重し、'
                '為替は動きやすくなります。見出しだけでなく「市場の予想とどうズレたか」が大事と言われます。')
    if group == 'vix':
        return ('**VIX（恐怖指数）**：投資家の不安の大きさを表す数字で、高いほど市場がびくびくしている状態です。'
                '急に上がると値動きも荒くなりやすいので、焦って動かず様子を見る人も多いです。')
    if group == 'usdjpy':
        if went_up:
            return ('**ドル円（円安方向）**：円の価値が下がる動きです。海外で稼ぐ輸出企業には追い風になりやすい一方、'
                    'ガソリンや食料品など輸入品は値上がりしやすくなります。「円安＝株高」と単純には決まりません。')
        return ('**ドル円（円高方向）**：円の価値が上がる動きです。輸入品は安くなりやすい一方、輸出企業の利益には'
                '逆風になりやすいと言われます。立場によって良し悪しが変わります。')
    if group == 'nikkei':
        return ('**日経平均の大きな動き**：まず「日本株全体の話」か「一部の大型株の話」かを切り分けると落ち着いて見られます。'
                'TOPIX（日本株全体）と比べるのがコツです。')
    if group == 'us10y':
        return ('**米国の長期金利**：世界のお金の基準になる金利です。上がると株（特に成長株）の重しや円安の要因になりやすい、'
                'と言われます。')
    if group == 'oil':
        return '**原油**：ガソリン・電気代・運送費を通じて、物価や企業の利益に響きます。'
    if group == 'gold':
        return '**金（ゴールド）**：不安なときに買われやすい「安全資産」と言われ、株が崩れる局面で上がることがあります。'
    if group == 'crypto':
        return ('**暗号資産**：株よりはるかに値動きが大きく、別の種類の資産として見るのが基本です。'
                '株の判断材料にそのまま使うのは難しいです。')
    if group == 'us_morning':
        return ('**毎朝の米国市場サマリー**：前の夜の米国株の結果です。日本株は朝の寄り付きで影響を受けやすいので、'
                '「今日の地合いのヒント」として読めます。')
    if group == 'calendar':
        return ('**今日の予定（権利付き最終日・SQ日・指標発表など）**：値が動きやすい日の目印です。'
                '予定を知っているだけで、急な値動きに驚きにくくなります。')
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
            comment = (f'{biggest_day.month}/{biggest_day.day}は{biggest:+.1f}%と大きく動きました。'
                       '1日で2%以上動く日は、国内の材料だけでなく海外市場・為替・金利などが重なっていることが多いです。')
        elif all(abs(p) < 1 for _, p in moves):
            comment = 'どの日も1%未満の小さな動きでした。こういう週は、日々の上下に一喜一憂しなくてよい週です。'
        elif len(moves) >= 3 and (all(p > 0 for _, p in moves) or all(p < 0 for _, p in moves)):
            comment = ('同じ向きの動きが続いた週です。続くときは背景に「理由」（金利・為替・ニュース）があることが多いので、'
                       '速報の内容と見比べてみましょう。')
        else:
            comment = '上げ下げが混ざった週でした。毎日の動きより「週全体でどちらに傾いたか」を見ると落ち着いて判断できます。'
        blocks.append(f'**📈 今週の日経平均（毎日の終わり値）**\n{strip}\n上がった日 {ups}日・下がった日 {downs}日\n{comment}')

    # --- 2) 今週出た速報の読み解き ---
    latest_by_group = {}
    for e in sorted(entries, key=lambda x: x.get('at_utc', '')):
        g = _CATEGORY_GROUP.get(e.get('category'))
        if g:
            latest_by_group[g] = e.get('text', '')
    guides = [_guide_for(g, latest_by_group[g]) for g in READING_GUIDE_PRIORITY if g in latest_by_group]
    guides = [g for g in guides if g][:READING_GUIDE_MAX_ITEMS]
    if guides:
        blocks.append('**🔍 今週の速報、こう読みます**\n' + '\n'.join(f'・{g}' for g in guides))
    elif not moves:
        blocks.append('今週は大きな動きの速報がありませんでした。「何も起きない週」もふつうにあります。')

    # --- 3) 考え方の3ステップ（毎週共通） ---
    blocks.append(
        '**🧭 毎日の情報を見るときの3ステップ**\n'
        '①何が動いた？（株・為替・金利・原油のどれか）\n'
        '②なぜ？（ニュース・発表・海外の動き。分からなければ「分からない」でOK）\n'
        '③自分に関係ある？（持っている銘柄・暮らしへの影響。関係なければ何もしない、も立派な選択です）'
    )
    blocks.append('※一般的な見方の紹介です。今後の値動きの予想や、売買のおすすめではありません。')
    return '\n\n'.join(blocks)


def build_weekly_news_text():
    """
    サロンメンバー向けの「今週の経済ニュース」。個別銘柄のシグナルとは別に、
    一般的な経済ニュースの見出し・リンクをそのまま列挙する（当システムによる
    要約・論評はしない。「出典が確認できない情報は載せない」方針に沿い、
    見出し自体を一次情報として扱う）。直近7日分・重複タイトル除去・最大
    WEEKLY_NEWS_MAX_ITEMS件。公開日が取れない記事も「取得できた時点で新着」と
    みなして対象に含める（取れないことを理由に有用な見出しを落とさない）。
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

    if not items:
        return '今週は経済ニュースの取得に失敗しました（フィード側の一時的な問題の可能性があります）。'

    lines = [f"・[{e['title']}]({e['link']})" if e.get('link') else f"・{e['title']}"
             for e in items[:WEEKLY_NEWS_MAX_ITEMS]]
    return "\n".join(lines)


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
    embeds = [
        {'title': '📰 今週の市場サマリー', 'description': build_market_narrative_text(nikkei_change, topix_change, snapshot), 'color': 0x2ECC71},
        {'title': '🌏 今週の経済ニュース', 'description': build_weekly_news_text(), 'color': 0x1ABC9C},
        {'title': '⚡ 今週あった速報', 'description': build_breaking_recap_text(), 'color': 0xE67E22},
        {'title': '🧭 今週の情報、どう考える？', 'description': build_reading_guide_text(nikkei_change), 'color': 0xE74C3C},
        {'title': '👀 機械が選んだ注目銘柄', 'description': build_signal_highlight_text(snapshot), 'color': 0x3498DB},
        {'title': '🎯 このシステムの成績（これまでの累計）', 'description': build_performance_recap_text(), 'color': 0x9B59B6},
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
