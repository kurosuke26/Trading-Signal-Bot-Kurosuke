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


def json_default(obj):
    """
    json.dump(s)用のフォールバック変換。pandas/numpy由来の値（np.bool_・np.int64等）が
    誤って払い出されても標準jsonが例外で落ちないようにするための保険。
    """
    if hasattr(obj, 'item'):  # numpyスカラー（np.bool_, np.int64, np.float64 等）
        return obj.item()
    return str(obj)


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
