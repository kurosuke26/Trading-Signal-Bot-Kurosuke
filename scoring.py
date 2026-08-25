#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scoring.py — ファンダメンタルズ＋テクニカル＋酒田五法を統合した複合スコア（Phase 2/3）

README.md に記載されていた配点方針をそのまま実装している:
  テクニカル   25%
  酒田五法     25%
  配当利回り   15%
  PER×PBR     20%
  EPS推移      10%
  グロース      5%
  ----------------
  合計        100%

【重要】データが取得できなかった項目は「0点」として扱うのではなく、母数（分母）から
除外して残りの項目で再配分する。dividendYield/PER/PBRのようにLONG/SHORTの一次判定に
使う必須項目が欠損している銘柄は、そもそもスクリーニング対象から除外する
（classify_stock 側で該当銘柄は 'SKIP' として扱う想定）。

スコア評価バンド（README準拠）:
  80点以上   : 割安度が極めて高い
  70-79点    : 割安度が高い
  60-69点    : やや割安
  50-59点    : 中立・監視対象
  50点未満   : 割高
"""

WEIGHTS = {
    'technical': 0.25,
    'sakata': 0.25,
    'dividend': 0.15,
    'per_pbr': 0.20,
    'eps': 0.10,
    'growth': 0.05,
}


def dividend_sub_score(div_yield):
    """配当利回り 3.5〜5.8% で満点。範囲外は近さに応じて部分点、大きく外れたら0点。"""
    if div_yield is None:
        return None
    if 3.5 <= div_yield <= 5.8:
        return 1.0
    distance = min(abs(div_yield - 3.5), abs(div_yield - 5.8))
    if distance <= 1.0:
        return 0.5 * (1 - distance)
    return 0.0


def per_pbr_sub_score(per_pbr):
    """PER×PBR ≦ 22.5 で部分点以上、15未満は超割安として満点。"""
    if per_pbr is None or per_pbr <= 0:
        return None
    if per_pbr < 15:
        return 1.0
    if per_pbr <= 22.5:
        # 15〜22.5の範囲を線形に 1.0 -> 0.6 で評価
        return 1.0 - 0.4 * (per_pbr - 15) / (22.5 - 15)
    return 0.0


def eps_sub_score(eps_trend):
    """直近3期のEPSが維持〜増加していれば満点、減少があれば0点。データなしはNone。"""
    if eps_trend is None:
        return None
    return 1.0 if eps_trend.get('increasing') else 0.0


def growth_sub_score(growth_value):
    """売上・利益成長率（yfinanceのearningsGrowth等、小数の割合値を想定）。正なら満点、負なら0点。"""
    if growth_value is None:
        return None
    if growth_value > 0.10:
        return 1.0
    if growth_value > 0:
        return 0.6
    return 0.0


def composite_score(dividend_yield, per_pbr, eps_trend, technical_score_val,
                     sakata_score_val, growth_value=None):
    """
    各サブスコア（0〜1、Noneは判定不能）から、README仕様の配点で0〜100点の総合スコアを算出する。

    戻り値: (score_0_100: float, breakdown: dict, available_weight_ratio: float)
      available_weight_ratio が低いほど「取得できたデータが少ない状態でのスコア」であることを示す。
      0.5未満（＝主要項目の半分以上が欠損）の場合は、呼び出し側で「参考値」として扱うことを推奨。
    """
    subs = {
        'dividend': dividend_sub_score(dividend_yield),
        'per_pbr': per_pbr_sub_score(per_pbr),
        'eps': eps_sub_score(eps_trend),
        'technical': technical_score_val,
        'sakata': sakata_score_val,
        'growth': growth_sub_score(growth_value),
    }

    available_weight = sum(WEIGHTS[k] for k, v in subs.items() if v is not None)
    if available_weight <= 0:
        return None, subs, 0.0

    weighted_sum = sum(WEIGHTS[k] * v for k, v in subs.items() if v is not None)
    # 欠損項目を除いた重みで正規化（＝取得できた項目だけで100点満点相当に再配分）
    score = (weighted_sum / available_weight) * 100

    return round(score, 1), subs, available_weight


def score_band_label(score):
    if score is None:
        return '判定不能'
    if score >= 80:
        return '割安度が極めて高い'
    if score >= 70:
        return '割安度が高い'
    if score >= 60:
        return 'やや割安'
    if score >= 50:
        return '中立・監視対象'
    return '割高'


def suggested_trade_levels(last_close, atr_value, atr_multiplier=1.5):
    """
    エントリー・初期損切り・トレーリングストップの目安を計算する。

    【方針】
    本バージョンではポジション（何日にいくらで買ったか）の永続管理は行わない
    （GitHub Actions実行間でのデータ保持の仕組みがまだ無いため）。
    そのため「トレーリングストップ」は具体的な決済執行ではなく、
    “エントリー後は「直近高値 − ATR×1.5」を毎日引き上げながら損切りラインとして
    使ってください” という運用ルールの提示にとどめている。

    戻り値:
      {
        'entry_price': float,
        'initial_stop': float or None,       # ATR取得不可時は None
        'initial_stop_pct': float or None,    # 初期損切り幅（%）
        'trailing_rule': str,                 # トレーリングストップの運用ルール文言
      }
    """
    result = {'entry_price': last_close, 'initial_stop': None,
              'initial_stop_pct': None, 'trailing_rule': None}
    if last_close is None:
        return result

    if atr_value is not None and atr_value > 0:
        stop = last_close - atr_value * atr_multiplier
        result['initial_stop'] = round(stop, 1)
        result['initial_stop_pct'] = round((atr_value * atr_multiplier / last_close) * 100, 2)
        result['trailing_rule'] = (
            f'初期損切り：¥{stop:,.0f}（ATR×{atr_multiplier}）／'
            f'以降は「その時点までの直近高値 − ATR×{atr_multiplier}」を目安に'
            f'損切りラインを切り上げてください（シャンデリア・ストップ方式）'
        )
    else:
        # ATRが計算できない銘柄向けの簡易フォールバック（%ベース）
        stop = last_close * 0.95
        result['initial_stop'] = round(stop, 1)
        result['initial_stop_pct'] = 5.0
        result['trailing_rule'] = (
            f'ATRが算出できなかったため簡易ルール：初期損切り目安 ¥{stop:,.0f}（-5%）／'
            f'ATRが算出可能になり次第、直近高値基準のトレーリングストップに切り替え推奨'
        )
    return result
