#!/usr/bin/env bash
# run_all_evaluations.sh — 検証を決まった順番で実行する（2026-09-16追加。再現性のため手順を固定）
#
# 前提：data/jquants_cache に backfill_jquants.py（上場銘柄）と backfill_delisted.py（上場廃止銘柄）の取得が済んでいること
# 出力：data/backtest_out/ の *_final.json / *.md と、各ステップのログ（run_all_*.log）
set -euo pipefail
export PYTHONIOENCODING=utf-8
OUT=data/backtest_out
mkdir -p "$OUT"
step() { echo "=== $(date '+%F %T') $1"; }

step "1/7 特徴量パネル"
python build_panel.py > "$OUT/run_all_panel.log" 2>&1
step "2/7 酒田五法（旧ロジック・比較用）"
python evaluate_sakata.py --impl legacy/sakata_legacy_20260915.py --out "$OUT/sakata_eval_legacy_final" > "$OUT/run_all_sakata_legacy.log" 2>&1
step "3/7 酒田五法（新ロジック）"
python evaluate_sakata.py --out "$OUT/sakata_eval_final" > "$OUT/run_all_sakata.log" 2>&1
step "4/7 売買シミュレーション（スクリーニング・酒田五法・出来高急増・ATR倍率）"
python evaluate_strategy.py --out "$OUT/strategy_eval_final" > "$OUT/run_all_strategy.log" 2>&1
step "5/7 投資家タイプ別モデル"
python evaluate_investor_styles.py > "$OUT/run_all_styles.log" 2>&1
step "6/7 増配"
python evaluate_dividend_hikes.py > "$OUT/run_all_hikes.log" 2>&1
step "7/7 暴落"
python evaluate_crashes.py > "$OUT/run_all_crashes.log" 2>&1
step "完了"
