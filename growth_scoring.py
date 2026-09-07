#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
growth_scoring.py — GROWTHシグナル（成長株）の複合スコア算出（2026-09-07新設）。

【設計の背景】
既存のLONGシグナルは「配当利回り3.5〜5.8%かつPER×PBR≦22.5」という割安×高配当専用の
ロジックで、無配・低PERでない成長株は構造上検知対象外になっていた（2026-09-06に
kurosukeさんと合意）。これを補うため、LONG/SHORTとは別に、複数の財務指標をスコア化して
合算する「GROWTH」シグナルを新設する。

【設計方針（2026-09-06〜09-07に合意した内容）】
- 個別のAND条件で足切りするのではなく、各指標を-2〜+2の5段階でスコア化し合算する
  （既存LONGのscoring.composite_scoreと同じ「データが取得できた項目だけで再配分する」
  という発想を踏襲。1項目でも欠損したら即NGにはしない）。
- 各指標の重みはまず均等割り（=どの指標も満点は同じ+2点）。バックテスト結果を見ながら
  重み付けを調整していく前提（v1）。
- 各指標の具体的な閾値は、2026-09-07にCAN SLIM（O'Neil）・Peter Lynch・マジック
  フォーミュラ（Greenblatt）・MSCI Qualityファクター・日本のグロース株スクリーニング
  実践例を調査し、その結果を踏まえてkurosukeさんと合意した値を使用している
  （詳細は`claude/2026-09-07-growth-investing-best-practices-research.md`・
  `claude/2026-09-07-growth-scoring-thresholds-proposal.md`を参照）。

【本モジュールが受け取るデータについて】
各サブスコア関数は「既に計算済みの値」を受け取るだけで、DataFrameの扱いやAPI呼び出しは
一切行わない（scoring.pyの設計方針を踏襲）。データの出どころは3系統に分かれる:
  1. fundamentals_jquants.build_fundamental_snapshot() が返す fundamental_snapshot辞書
     （売上高成長率・営業利益成長率・経常利益成長率・EPSトレンド・ROE・自己資本比率・
     ROA・PSR・PER）
  2. indicators.compute_trading_value_momentum() が返す比率（価格・出来高ベース）
  3. theme_news.py のスナップショット（data/theme_news_snapshot.json）が持つ
     momentum_ratio（ニュース・テーマ由来）
"""

# ---------------------------------------------------------------------------
# PSRのセクター調整：薄利多売・伝統的業種は「厳しめグループ」として上限を下げる
# （2026-09-07にkurosukeさんと合意したたたき台の業種分け）
# ---------------------------------------------------------------------------
PSR_STRICT_SECTORS = {
    '小売業', '食料品', '卸売業', '陸運業', '倉庫・運輸関連業',
}


def _tier_score(value, breakpoints):
    """
    breakpoints: [(閾値, スコア), ...] を「値がこの閾値以上ならこのスコア」の順（高い方から）
    で並べたリスト。どれにも該当しなければ最後のスコア（最低点）を返す。
    value が None なら None（判定不能、母数から除外）を返す。
    """
    if value is None:
        return None
    for threshold, score in breakpoints[:-1]:
        if value >= threshold:
            return score
    return breakpoints[-1][1]


# ---------------------------------------------------------------------------
# 各指標のサブスコア関数
# ---------------------------------------------------------------------------
def sales_growth_sub_score(value):
    """売上高成長率（YoY・3年平均CAGR共通）。単位は%（例：20.0 = 20%）。"""
    return _tier_score(value, [(20, 2), (15, 1), (5, 0), (0, -1), (float('-inf'), -2)])


def profit_growth_sub_score(value):
    """営業利益成長率・経常利益成長率共通（YoY、単位%）。利益は売上より振れやすいため
    やや広めのバンドを設定（CAN SLIM/Lynchの「25%以上の利益成長」を満点の目安に）。"""
    return _tier_score(value, [(25, 2), (15, 1), (0, 0), (-10, -1), (float('-inf'), -2)])


def eps_trend_sub_score(eps_trend):
    """eps_trend: 'up_3q' / 'flat_3q' / 'down_3q' / None（fundamentals_jquants._eps_trend参照）。"""
    if eps_trend is None:
        return None
    return {'up_3q': 2, 'flat_3q': 0, 'down_3q': -2}.get(eps_trend)


def roe_sub_score(value):
    """ROE（単位%）。2026-09-07にkurosukeさんと確認済みの引き上げ後の基準
    （Lynch/CAN SLIM/MSCI Qualityが揃って「15%以上、理想20%以上」としていることに合わせた）。"""
    return _tier_score(value, [(20, 2), (15, 1), (8, 0), (3, -1), (float('-inf'), -2)])


def equity_ratio_sub_score(value):
    """自己資本比率（単位%）。2026-09-07に新規追加で合意済み（レバレッジの低さ＝財務健全性）。"""
    return _tier_score(value, [(60, 2), (40, 1), (30, 0), (20, -1), (float('-inf'), -2)])


def roa_sub_score(value):
    """ROA（総資産利益率、単位%）。"""
    return _tier_score(value, [(10, 2), (5, 1), (2, 0), (0, -1), (float('-inf'), -2)])


def peg_sub_score(value):
    """PEGレシオ（PER÷利益成長率）。Lynch・CAN SLIM日本応用・GFS Tokyoが揃って
    1.0〜1.5倍を目安にしていることに合わせた。値が無い、または負値（成長率マイナス等で
    PEGの意味が成立しない）場合はNoneではなく最低点（-2）扱いにする方が安全だが、
    ここでは他の指標と同様「算出不能＝判定不能」としてNone（母数除外）を採用している
    （PEGが計算できない銘柄は大抵他の成長指標側で既にマイナス評価が付くため、
    二重にペナルティを科す必要は薄いという判断。運用しながら見直す余地あり）。"""
    if value is None or value <= 0:
        return None
    return _peg_breakpoints(value)


def _peg_breakpoints(value):
    if value <= 1.0:
        return 2
    if value <= 1.5:
        return 1
    if value <= 2.0:
        return 0
    if value <= 3.0:
        return -1
    return -2


def psr_sub_score(value, sector_name=None):
    """PSR（株価売上高倍率）。sector_nameがPSR_STRICT_SECTORSに該当する業種は
    薄利多売の伝統的業種とみなし、より厳しい基準を適用する（2026-09-07合意のたたき台）。"""
    if value is None:
        return None
    if sector_name in PSR_STRICT_SECTORS:
        return _psr_strict_score(value)
    return _psr_standard_score(value)


def _psr_strict_score(value):
    if value <= 1:
        return 2
    if value <= 2:
        return 1
    if value <= 5:
        return 0
    if value <= 7:
        return -1
    return -2


def _psr_standard_score(value):
    if value <= 3:
        return 2
    if value <= 5:
        return 1
    if value <= 10:
        return 0
    if value <= 15:
        return -1
    return -2


def trading_value_momentum_sub_score(ratio):
    """直近20日平均売買代金 ÷ 直前60日平均（indicators.compute_trading_value_momentumの戻り値）。"""
    return _tier_score(ratio, [(2.0, 2), (1.5, 1), (0.8, 0), (0.5, -1), (float('-inf'), -2)])


def news_momentum_sub_score(momentum_ratio, has_news_mention):
    """
    theme_news.pyのmomentum_ratio。2026-09-07にkurosukeさんと合意済みの方針として、
    「該当ニュースが無い銘柄」は減点せずNone（判定不能・母数除外）として扱う
    （大多数の銘柄はそもそも政策ニュースに無縁なため、無いことを弱気シグナルにはしない）。
    """
    if not has_news_mention or momentum_ratio is None:
        return None
    return _tier_score(momentum_ratio, [(3.0, 2), (1.5, 1), (0.7, 0), (0.3, -1), (float('-inf'), -2)])


# ---------------------------------------------------------------------------
# 複合スコア
# ---------------------------------------------------------------------------
def growth_composite_score(fundamental_snapshot, trading_value_momentum=None,
                            news_momentum_ratio=None, has_news_mention=False):
    """
    GROWTHシグナルの複合スコア（-2〜+2の各サブスコアを均等加重で合算し、0〜100点に換算）。

    戻り値: (score_0_100: float or None, breakdown: dict, available_count: int)
      available_count はデータが取得できた指標の数（分母）。0の場合はscoreもNone。
      breakdown には各指標のサブスコア（-2〜+2、またはNone＝判定不能）を格納する。
    """
    fs = fundamental_snapshot or {}
    sector_name = fs.get('sector_name')

    subs = {
        'sales_growth_yoy': sales_growth_sub_score(fs.get('sales_growth_yoy')),
        'sales_growth_3y_avg': sales_growth_sub_score(fs.get('sales_growth_3y_avg')),
        'op_growth_yoy': profit_growth_sub_score(fs.get('op_growth_yoy')),
        'ordinary_profit_growth_yoy': profit_growth_sub_score(fs.get('ordinary_profit_growth_yoy')),
        'eps_trend': eps_trend_sub_score(fs.get('eps_trend')),
        'roe': roe_sub_score(fs.get('roe')),
        'equity_ratio': equity_ratio_sub_score(fs.get('equity_ratio')),
        'roa': roa_sub_score(fs.get('roa')),
        'peg': peg_sub_score(_compute_peg(fs)),
        'psr': psr_sub_score(fs.get('psr'), sector_name=sector_name),
        'trading_value_momentum': trading_value_momentum_sub_score(trading_value_momentum),
        'news_momentum': news_momentum_sub_score(news_momentum_ratio, has_news_mention),
    }

    available = [v for v in subs.values() if v is not None]
    if not available:
        return None, subs, 0

    raw_sum = sum(available)
    max_possible = len(available) * 2  # 各指標の満点は+2
    score = 50 + (raw_sum / max_possible) * 50
    return round(score, 1), subs, len(available)


def _compute_peg(fundamental_snapshot):
    """
    PEG = PER ÷ 利益成長率(%)。fundamental_snapshotにはPERはあるがPEGそのものは
    無いため、ここで組み立てる。利益成長率は営業利益成長率を優先し、無ければ
    売上高成長率にフォールバックする（EPS成長率そのものは3期の「向き」しか
    fundamentals_jquants側で持っていないため、数値としてはop_growth_yoyを代用する）。
    """
    fs = fundamental_snapshot or {}
    per = fs.get('per')
    growth = fs.get('op_growth_yoy')
    if growth is None:
        growth = fs.get('sales_growth_yoy')
    if per is None or per <= 0 or growth is None or growth <= 0:
        return None
    return round(per / growth, 2)


def growth_score_band_label(score):
    if score is None:
        return '判定不能'
    if score >= 80:
        return '成長性が極めて高い'
    if score >= 70:
        return '成長性が高い'
    if score >= 60:
        return 'やや成長性あり'
    if score >= 50:
        return '中立・監視対象'
    return '成長性は限定的'


if __name__ == '__main__':
    """
    動作確認用：collector.pyの本番パイプライン（yfinance取得等）を通さずに、
    J-Quantsキャッシュ済みの実データだけでGROWTHスコアがどう出るかを確認できる。

    使い方: python growth_scoring.py [銘柄コード4桁 ...]
      （省略時はトヨタ(7203)・ソニーG(6758)など数銘柄で試す）
    market_cap・sector_name・current_priceはキャッシュに無いため、この確認スクリプト内では
    ダミー値を使う（PSRの絶対値やPER関連の一部指標は参考値になる点に注意。あくまで
    「エラーなく動くか」「各サブスコアが妥当な値になるか」の確認が目的）。
    """
    import sys
    from fundamentals_jquants import build_fundamental_snapshot

    tickers = sys.argv[1:] or ['7203', '6758', '9984', '6861']
    for t in tickers:
        snap = build_fundamental_snapshot(
            f'{t}.T', current_price=1000, sector_code=None, sector_name=None,
            dividend_yield=0, market_cap=1_000_000_000_000,  # ダミー：1兆円
        )
        score, breakdown, n = growth_composite_score(snap)
        print(f'--- {t} ---')
        print(f'  fundamental_snapshot: {snap}')
        print(f'  GROWTHスコア: {score}（{growth_score_band_label(score)}）判定可能項目数={n}')
        print(f'  内訳: {breakdown}')
