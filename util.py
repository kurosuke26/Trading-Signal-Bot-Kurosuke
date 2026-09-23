#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
util.py — collector.py / poster.py で共有する小さなユーティリティ群。

- 環境変数のパース（未設定・空文字・不正値でも落ちないようにする）
- JSON化できないnumpy/pandas由来の値の変換フォールバック
- 「N個の処理をtarget_seconds秒に均等に引き延ばす」ためのペーシング関数
  （2:00〜6:00 JSTのような“じっくり収集する時間帯”に、Yahoo! Financeへの
  リクエストを一気に送らず、意図的に間隔を空けて負荷を均すために使う）
"""

import os
import time


def env_int(name, default):
    """
    環境変数を整数として読む。未設定・空文字・不正値はdefaultにフォールバックする。
    【注意】GitHub Actionsのworkflowでworkflow_dispatchの入力を
    `${{ github.event.inputs.xxx }}` として渡している場合、scheduleトリガー
    （毎日の自動実行）ではinputsが存在せず空文字が渡ってくる。int('')はValueErrorに
    なるため、素朴に int(os.getenv(...)) と書くと毎日の自動実行が全て落ちてしまう。
    """
    raw = os.getenv(name)
    if raw is None or raw.strip() == '':
        return default
    try:
        return int(raw)
    except ValueError:
        print(f'[config] 環境変数 {name}={raw!r} を整数として解釈できないため既定値{default}を使用します')
        return default


def env_float(name, default):
    raw = os.getenv(name)
    if raw is None or raw.strip() == '':
        return default
    try:
        return float(raw)
    except ValueError:
        print(f'[config] 環境変数 {name}={raw!r} を数値として解釈できないため既定値{default}を使用します')
        return default


def safe_num(value):
    """
    yfinanceのinfo辞書は、特定の銘柄（優先株・ETN・新しい英数字ティッカーコード等）で
    まれに本来float/intのはずのフィールドがリストなど数値以外の型で返ってくることが
    ある（2026-08-26の本番実行で407A.T等6銘柄が該当）。「数値×リスト」のような
    TypeErrorで銘柄1件の判定処理全体が落ちてしまうのを防ぐため、int/float以外は
    None（未取得扱い）として安全に丸める。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def json_default(obj):
    """
    json.dump(s)用のフォールバック変換。pandas/numpy由来の値（np.bool_・np.int64等）が
    誤って払い出されても標準jsonが例外で落ちないようにするための保険。
    """
    if hasattr(obj, 'item'):  # numpyスカラー（np.bool_, np.int64, np.float64 等）
        return obj.item()
    return str(obj)


# ---------------------------------------------------------------------------
# 株式分割への対応（2026-09-23追加）
# ---------------------------------------------------------------------------
# 株価は yfinance の auto_adjust=True（分割・配当調整済み）で取得しており、分割が起きると
# 過去の株価が遡って調整される。一方、記録済みの取得価格やストップは分割前の水準のままなので、
# 放置すると「1:2分割で株価が半分になった＝50%下落」と誤認する（ロングは即ストップ、
# ショートは含み益が倍に見える）。そこで、記録した当時の株価と、同じ日付の今回の株価を
# 比べて倍率を求め、記録側を同じ水準に揃える。
PRICE_ADJUST_TOLERANCE = 0.005      # 同じ日付どうしの比較なので、0.5%以上ずれていれば調整とみなす（分割・配当落ち）
PRICE_ADJUST_FACTOR_MIN = 0.05      # 1:20分割相当まで
PRICE_ADJUST_FACTOR_MAX = 20.0      # 20:1併合相当まで。これを外れる倍率はデータ異常として無視する


def price_adjust_factor(recorded_price, current_series_price):
    """
    記録した株価と、同じ日付の最新データの株価から調整倍率を返す（調整不要ならNone）。
    データ異常（例：1909.Tが約163億円で配信された）に引きずられないよう、常識的な範囲外はNoneにする。
    """
    if not recorded_price or not current_series_price or recorded_price <= 0 or current_series_price <= 0:
        return None
    f = current_series_price / recorded_price
    if abs(f - 1) <= PRICE_ADJUST_TOLERANCE:
        return None
    if not PRICE_ADJUST_FACTOR_MIN <= f <= PRICE_ADJUST_FACTOR_MAX:
        return None
    return f


def pace_to_target(index, total, phase_start_time, target_seconds,
                    now_fn=time.time, sleep_fn=time.sleep):
    """
    「total個のチャンク処理を、target_seconds秒かけて均等に終わらせる」ためのペーシング。

    各チャンク処理が終わるたびに呼び出す。実際の処理が速く終わっていれば、
    「本来この時点で経過しているべき時間」との差分だけ意図的にsleepする。
    処理が遅れている（もう間に合わない）場合は何もしない（追加で遅らせない）。

    例：3900銘柄・チャンクサイズ50 → 78チャンク、target_seconds=150分=9000秒なら、
    1チャンクあたり平均約115秒のペースになるよう自動的に間隔を調整する。
    target_seconds<=0 の場合はペーシングを無効化（できるだけ速く処理）する。
    """
    if target_seconds <= 0 or total <= 0:
        return
    target_elapsed = (index + 1) / total * target_seconds
    actual_elapsed = now_fn() - phase_start_time
    remaining = target_elapsed - actual_elapsed
    if remaining > 0:
        sleep_fn(remaining)
