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
     LONG＋SHORT合算／LONGのみ／SHORTのみ（自信度上位10銘柄）の勝率・
     ペイオフレシオの週次チェックポイント
  4. 投資理論・スクリーニング手法の一言解説（週替わりで固定トピックを順番に紹介）
"""

import json
import os
import sys
from datetime import datetime, timezone

import yfinance as yf

import poster
from tracking import load_trade_log, compute_performance_stats

SNAPSHOT_PATH = os.getenv('SCAN_OUTPUT_PATH') or 'data/latest_scan.json'

NIKKEI_TICKER = '^N225'
# TOPIX指数そのもの(998405.T)が取得できない環境向けに、連動ETF(1306.T)へ
# フォールバックする。
TOPIX_TICKERS = ['998405.T', '1306.T']

TIPS = [
    {
        'title': '📖 TIPS：配当利回り 3.5〜5.8% に限定する理由',
        'body': (
            '配当利回りが低すぎる銘柄は株価が既に評価されすぎている可能性が、'
            '逆に高すぎる（8%超など）銘柄は業績悪化による株価下落や減配リスクを'
            '織り込んでいる可能性があります。Kurosukeでは「割安さと持続性のバランス」'
            'が取れやすい3.5〜5.8%のレンジをLONG候補の一次条件にしています。'
        ),
    },
    {
        'title': '📖 TIPS：PER × PBR の組み合わせの意味',
        'body': (
            'PER（株価収益率）は「利益に対する割安さ」、PBR（株価純資産倍率）は'
            '「純資産に対する割安さ」を示します。片方だけでは業種特性に引っ張られ'
            'やすいため、Kurosukeでは「PER×PBR ≦ 22.5」を目安にすることで、'
            '収益面・資産面の両方から見て割安な銘柄を絞り込んでいます。'
        ),
    },
    {
        'title': '📖 TIPS：酒田五法パターンの見方',
        'body': (
            '酒田五法は江戸時代の米相場から生まれたローソク足分析の体系で、'
            '三山・三川・三兵・三空・三法など複数のパターンがあります。'
            'Kurosukeが表示する「参考精度」は引き継ぎ資料に記載されていた数値'
            'であり、実測のバックテスト結果ではありません。あくまで「他の根拠'
            '（テクニカル・ファンダメンタルズ）と組み合わせて補強材料にする」'
            '使い方をおすすめします。'
        ),
    },
    {
        'title': '📖 TIPS：トレーリングストップ（ATR×1.5）の考え方',
        'body': (
            'ATR（Average True Range）は値動きの荒さを表す指標です。'
            'Kurosukeは「直近高値 − ATR×1.5」を損切りラインとして毎日切り上げる'
            '（シャンデリア・ストップ方式）ことで、値動きの荒い銘柄では損切り幅を'
            '広めに、落ち着いた銘柄では狭めに、自動的に調整しています。'
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
    ショートシグナル比率から、相場全体の過熱感・警戒感をひとことで表す文章を作る。
    しきい値（15%/30%）はposter.py側の市場警戒ロジックと揃えている。
    """
    if short_ratio is None or not analyzed:
        return '銘柄ごとのシグナル集計が取得できなかったため、相場全体の温度感は省略します。'
    if short_ratio >= 30:
        mood = (f'分析できた{analyzed}銘柄中{short_ratio:.1f}%が割高警戒（ショート）シグナルとなっており、'
                f'相場全体にやや過熱感がうかがえる1週間でした。')
    elif short_ratio >= 15:
        mood = (f'分析できた{analyzed}銘柄中{short_ratio:.1f}%が割高警戒（ショート）シグナルで、'
                f'過熱感・割安感ともに標準的な範囲内の1週間でした。')
    else:
        mood = (f'分析できた{analyzed}銘柄中、割高警戒（ショート）シグナルは{short_ratio:.1f}%にとどまり、'
                f'過熱感は限定的な1週間でした。')
    mood += f' 一方、割安候補（ロングシグナル）は{long_count}銘柄検出されています。'
    return mood


def build_market_narrative_text(nikkei_change, topix_change, snapshot):
    """
    日経平均・TOPIXの週間騰落率と、直近収集時点のシグナル比率から、ルールベースで
    「今週の市場サマリー」文章を自動生成する。

    【正直な注意点】
    これは実際のニュース記事や市況解説の要約ではない。当システムが取得できる数値
    （指数騰落率・自前のシグナル判定比率）だけから、あらかじめ用意した文章
    テンプレートを機械的に組み合わせた文章であり、個別の決算・金融政策・海外市況
    などの材料には一切触れていない。
    """
    lines = [
        f'日経平均は{_index_trend_phrase(nikkei_change)}、'
        f'TOPIXは{_index_trend_phrase(topix_change)}という1週間でした。'
    ]

    counts = (snapshot or {}).get('counts', {})
    analyzed = counts.get('analyzed', 0)
    long_count = counts.get('LONG', 0)
    short_count = counts.get('SHORT', 0)
    short_ratio = (short_count / analyzed * 100) if analyzed else None

    lines.append(_breadth_phrase(short_ratio, long_count, analyzed))
    lines.append(
        '※このサマリーは実際のニュースではなく、指数の騰落率とシグナル比率のみから'
        '自動生成した文章です（個別の決算・金融政策・海外市況等は反映していません）。'
    )
    return "\n".join(lines)


def build_signal_highlight_text(snapshot):
    if not snapshot:
        return '直近の収集データが見つからないため、シグナル紹介は省略します。'

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
        score_text = f"{score:.1f}点" if score is not None else '判定不能'
        lines.append(f"注目LONG：{top['ticker']}（{top.get('name')}）スコア{score_text}")
    if short_results:
        top = short_results[0]
        per_pbr = top.get('per_pbr')
        per_pbr_text = f"{per_pbr:.1f}" if per_pbr is not None else '不明'
        lines.append(f"注目SHORT（警戒）：{top['ticker']}（{top.get('name')}）PER×PBR {per_pbr_text}")
    if not lines:
        lines.append('直近の収集では該当するLONG/SHORTシグナルがありませんでした。')

    counts = snapshot.get('counts', {})
    lines.append(
        f"直近の収集：LONG {counts.get('LONG', 0)}銘柄／SHORT {counts.get('SHORT', 0)}銘柄"
        f"／NEUTRAL {counts.get('NEUTRAL', 0)}銘柄"
    )
    lines.append('※直近の収集時点のスナップショットに基づく紹介です（1週間分の履歴集計ではありません）')
    return "\n".join(lines)


def build_performance_recap_text():
    trade_log = load_trade_log()
    stats = compute_performance_stats(trade_log)

    def _fmt(label, s):
        if not s or not s.get('closed_count'):
            return f'{label}：決済済み取引がまだありません（集計中）'
        parts = [f"決済{s['closed_count']}件", f"勝率{s['win_rate']}%"]
        if s.get('payoff_ratio') is not None:
            parts.append(f"ペイオフレシオ{s['payoff_ratio']}")
        return f"{label}：" + "／".join(parts)

    return "\n".join([
        _fmt('LONG＋SHORT合算（上位10銘柄・累計）', stats['long_short']),
        _fmt('LONGのみ（上位10銘柄・累計）', stats['long_only']),
        _fmt('SHORTのみ（上位10銘柄・累計）', stats['short_only']),
        f"現在保有中（未決済）：{stats['open_positions']}件",
    ])


def pick_weekly_tip():
    week_number = datetime.now(timezone.utc).isocalendar()[1]
    return TIPS[week_number % len(TIPS)]


def build_payload():
    tip = pick_weekly_tip()
    nikkei_change = fetch_weekly_change(NIKKEI_TICKER)
    topix_change, _ = fetch_topix_change()
    snapshot = load_latest_snapshot()
    embeds = [
        {'title': '📰 今週の市場サマリー', 'description': build_market_narrative_text(nikkei_change, topix_change, snapshot), 'color': 0x2ECC71},
        {'title': '👀 直近の注目シグナル', 'description': build_signal_highlight_text(snapshot), 'color': 0x3498DB},
        {'title': '🎯 シグナル成績（累計）', 'description': build_performance_recap_text(), 'color': 0x9B59B6},
        {'title': tip['title'], 'description': tip['body'], 'color': 0x95A5A6},
    ]
    return {
        'content': '📅 Kurosuke割安チェッカー - 週次Tips',
        'embeds': embeds[:10],
    }


def post_weekly_tips():
    print("=" * 80)
    print("Kurosuke 週次Tips投稿バッチ（weekly_tips.py）")
    print("=" * 80)
    payload = build_payload()
    return poster.send_discord_message('TIPS', payload)


if __name__ == '__main__':
    try:
        success = post_weekly_tips()
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"Error: {str(e)}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
