#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
poster.py — Discord投稿バッチ（7:00〜8:00 JST頃に実行する想定）

collector.py が深夜のうちに作成した data/latest_scan.json を読み込み、5チャネル
（ロング/ショート/警告/パフォーマンス/戦略通知）に投稿する。Yahoo! Financeへの
アクセスは行わないため、実行時間は短く（数秒〜数十秒程度）、レート制限のリスクも
ほぼない。

【データが無い・古い場合の扱い】
collect_data.yml（collector.py側のワークフロー）が失敗した、もしくは間に合わなかった
場合に、無言でスキップしたり古いデータをさも最新のように投稿したりしないよう、
- data/latest_scan.json が存在しない → WARNINGチャネルにその旨を投稿して終了
- 生成時刻が MAX_SNAPSHOT_AGE_HOURS（既定12時間）より古い → 投稿はするが、
  LONG/SHORT/戦略通知の本文とWARNINGに「データが古い可能性がある」旨を明記する
という扱いにしている。
"""

import csv
import io
import json
import os
import sys
from datetime import datetime, timezone

import requests

from scoring import score_band_label, suggested_trade_levels
from tracking import ATR_MULTIPLIER_BY_SIGNAL, ATR_MULTIPLIER_VARIANTS, _variant_key
from util import env_float, json_default

SNAPSHOT_PATH = os.getenv('SCAN_OUTPUT_PATH') or 'data/latest_scan.json'
MAX_SNAPSHOT_AGE_HOURS = env_float('MAX_SNAPSHOT_AGE_HOURS', 12)

# ---------------------------------------------------------------------------
# Webhook設定（従来から変更なし）
# ---------------------------------------------------------------------------
CHANNELS = ['LONG', 'SHORT', 'WARNING', 'PERFORMANCE', 'STRATEGY', 'BACKTEST', 'TIPS']
ENVIRONMENTS = ['TEST', 'PROD']

CHANNEL_LABELS = {
    'LONG': 'ロング-シグナル',
    'SHORT': 'ショート-シグナル',
    'WARNING': 'マーケット警告',
    'PERFORMANCE': 'パフォーマンス',
    'STRATEGY': '戦略通知',
    'BACKTEST': 'バックテスト結果',  # 勝率・ペイオフレシオ（tracking.py）専用チャンネル
    'TIPS': '週次tips',              # weekly_tips.py がここに投稿（poster.py本体は使わない）
}

WEBHOOKS = {}
for _ch in CHANNELS:
    for _env in ENVIRONMENTS:
        WEBHOOKS[(_ch, _env)] = os.getenv(f'DISCORD_WEBHOOK_URL_{_ch}_{_env}')

for _env in ENVIRONMENTS:
    if not WEBHOOKS[('STRATEGY', _env)]:
        WEBHOOKS[('STRATEGY', _env)] = os.getenv(f'DISCORD_WEBHOOK_URL_SAKATA_{_env}')

LEGACY_WEBHOOK_URL = os.getenv('DISCORD_WEBHOOK_URL')
_ANY_NEW_WEBHOOK_SET = any(WEBHOOKS.values())


# ---------------------------------------------------------------------------
# Discord Embed整形
# ---------------------------------------------------------------------------
SIGNAL_COLOR = {'LONG': 0x2ECC71, 'SHORT': 0xE74C3C, 'NEUTRAL': 0x95A5A6}
SIGNAL_LABEL = {'LONG': '🟢 買いシグナル', 'SHORT': '🔴 ショート注意', 'NEUTRAL': '⚪ ニュートラル'}


def _ma_rsi_macd_line(tech_snapshot):
    tech_snapshot = tech_snapshot or {}
    ma = tech_snapshot.get('ma') or {}
    macd = tech_snapshot.get('macd') or {}
    trend_label = {'bullish': '強気配列', 'bearish': '弱気配列', 'mixed': '方向感なし', None: '判定不能'}
    rsi = tech_snapshot.get('rsi')
    rsi_str = f"{rsi:.1f}" if rsi is not None else 'N/A'
    macd_hist = macd.get('hist')
    macd_str = f"{macd_hist:+.2f}" if macd_hist is not None else 'N/A'
    cross = ''
    if ma.get('golden_cross_recent'):
        cross = '（直近GC）'
    elif ma.get('dead_cross_recent'):
        cross = '（直近DC）'
    return f"MA:{trend_label.get(ma.get('trend'))}{cross} / RSI:{rsi_str} / MACDヒスト:{macd_str}"


def format_stock_embed(r):
    """
    LONG/SHORT/上位ニュートラルのみ 'detail': 'full' としてcollector側で保存されている。
    format_stock_embedはfullな結果に対してのみ呼び出す想定だが、念のためtech_snapshot等が
    無くても落ちないようガードしている。
    """
    per_pbr = r.get('per_pbr')
    per_pbr_str = f"{per_pbr:.2f}" if per_pbr is not None else "N/A"
    title = f"{'🔥 ' if r.get('super_cheap') else ''}{r['ticker']}（{r.get('name')}）"
    score = r.get('score')
    band = score_band_label(score)
    score_str = f"{score:.1f}/100（{band}）" if score is not None else '判定不能'
    current_price = r.get('current_price') or 0
    dividend_yield = r.get('dividend_yield') or 0

    fields = [
        {'name': '現在値', 'value': f"¥{current_price:,.0f}", 'inline': True},
        {'name': '配当利回り', 'value': f"{dividend_yield:.2f}%", 'inline': True},
        {'name': 'PER×PBR', 'value': per_pbr_str, 'inline': True},
        {'name': '複合スコア', 'value': score_str, 'inline': True},
        {'name': 'テクニカル', 'value': _ma_rsi_macd_line(r.get('tech_snapshot')), 'inline': False},
    ]

    sakata_reasons = r.get('sakata_reasons') or []
    if sakata_reasons:
        fields.append({'name': '酒田五法', 'value': '\n'.join(sakata_reasons[:2])[:1000], 'inline': False})

    eps_trend = r.get('eps_trend')
    if eps_trend is not None:
        vals = " → ".join(f"{v:.1f}" for v in eps_trend['values'])
        status = "増加傾向 ✅" if eps_trend['increasing'] else "減少あり ⚠️"
        fields.append({'name': 'EPS3期推移', 'value': f"{vals}（{status}）", 'inline': False})

    if r.get('signal') == 'LONG':
        atr = (r.get('tech_snapshot') or {}).get('atr')
        trade = suggested_trade_levels(current_price, atr, atr_multiplier=ATR_MULTIPLIER_BY_SIGNAL['LONG'])
        entry_label = '🟢 エントリー好機' if r.get('entry_timing') == 'good' else '🟡 条件達成・タイミング待ち'
        trade_lines = [entry_label]
        if trade.get('initial_stop') is not None:
            trade_lines.append(f"初期損切り目安：¥{trade['initial_stop']:,.0f}（-{trade['initial_stop_pct']}%）")
        if trade.get('trailing_rule'):
            trade_lines.append(trade['trailing_rule'])
        fields.append({'name': 'エントリー・ストップ目安', 'value': '\n'.join(trade_lines)[:1000], 'inline': False})

    return {
        'title': title,
        'description': SIGNAL_LABEL.get(r.get('signal'), r.get('signal')),
        'color': SIGNAL_COLOR.get(r.get('signal'), 0x95A5A6),
        'fields': fields,
    }


def build_csv_bytes(results, columns):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(columns)
    for r in results:
        writer.writerow([r.get(c, '') for c in columns])
    return buf.getvalue().encode('utf-8-sig')  # Excelでの文字化け防止のためBOM付き


CSV_COLUMNS = ['ticker', 'name', 'current_price', 'dividend_yield', 'per_pbr', 'score', 'signal', 'entry_timing']


# ---------------------------------------------------------------------------
# Discord送信
# ---------------------------------------------------------------------------
def get_webhook_urls(channel):
    urls = []
    for env in ENVIRONMENTS:
        url = WEBHOOKS.get((channel, env))
        if url:
            urls.append((env, url))
    return urls


def send_discord_message(channel, payload, file_bytes=None, filename=None):
    if payload is None:
        return True

    urls = get_webhook_urls(channel)
    if not urls:
        if not _ANY_NEW_WEBHOOK_SET and LEGACY_WEBHOOK_URL:
            urls = [('LEGACY', LEGACY_WEBHOOK_URL)]
        else:
            print(f"[{CHANNEL_LABELS[channel]}] Webhook URL が未設定のためスキップ")
            return False

    all_ok = True
    for env_label, url in urls:
        try:
            body = json.dumps(payload, ensure_ascii=False, default=json_default)
            if file_bytes is not None:
                files = {'file': (filename or 'data.csv', io.BytesIO(file_bytes), 'text/csv')}
                data = {'payload_json': body}
                response = requests.post(url, data=data, files=files)
            else:
                response = requests.post(url, data=body.encode('utf-8'),
                                          headers={'Content-Type': 'application/json'})

            if response.status_code in (200, 204):
                print(f"[{CHANNEL_LABELS[channel]}/{env_label}] 送信しました")
            else:
                print(f"[{CHANNEL_LABELS[channel]}/{env_label}] 送信失敗：{response.status_code}：{response.text[:200]}")
                all_ok = False
        except Exception as e:
            print(f"[{CHANNEL_LABELS[channel]}/{env_label}] Error: {str(e)}")
            all_ok = False

    return all_ok


def send_simple_warning(message):
    return send_discord_message('WARNING', {
        'embeds': [{'title': '⚠️ Kurosuke割安チェッカー（poster.py）', 'description': message[:4000], 'color': 0xF39C12}],
    })


# ---------------------------------------------------------------------------
# スナップショット読み込み・鮮度チェック
# ---------------------------------------------------------------------------
def load_snapshot(path=None):
    """
    【注意】デフォルト引数を path=SNAPSHOT_PATH としてしまうと、関数定義時点の値が
    固定されてしまい、モジュール属性 poster.SNAPSHOT_PATH を後から書き換えても
    （テストでのmock.patch.object等）反映されない。そのため呼び出し時に毎回
    モジュールグローバルを参照する形にしている。
    """
    if path is None:
        path = SNAPSHOT_PATH
    if not os.path.exists(path):
        return None, None
    with open(path, encoding='utf-8') as f:
        snapshot = json.load(f)

    age_hours = None
    generated_at = snapshot.get('generated_at_utc')
    if generated_at:
        try:
            generated_dt = datetime.fromisoformat(generated_at)
            age_hours = (datetime.now(timezone.utc) - generated_dt).total_seconds() / 3600.0
        except ValueError:
            pass
    return snapshot, age_hours


# ---------------------------------------------------------------------------
# ペイロード組み立て
# ---------------------------------------------------------------------------
def build_payloads(snapshot, stale, age_hours):
    results = snapshot.get('results', {})
    counts = snapshot.get('counts', {})
    fund_failed = [tuple(x) for x in snapshot.get('fund_failed', [])]
    hist_failed = [tuple(x) for x in snapshot.get('hist_failed', [])]
    universe_size = snapshot.get('universe_size', 0)
    used_fallback = snapshot.get('used_fallback', False)
    elapsed_minutes = snapshot.get('elapsed_minutes', 0)
    generated_at = snapshot.get('generated_at_utc', '')
    performance_stats = snapshot.get('performance_stats')

    full_results = [r for r in results.values() if r.get('detail') == 'full']
    long_results = sorted([r for r in full_results if r['signal'] == 'LONG'],
                           key=lambda r: (r['score'] if r['score'] is not None else -1), reverse=True)
    short_results = sorted([r for r in full_results if r['signal'] == 'SHORT'],
                            key=lambda r: (r['per_pbr'] if r['per_pbr'] is not None else 0), reverse=True)
    neutral_results = sorted([r for r in full_results if r['signal'] == 'NEUTRAL'],
                              key=lambda r: (r['score'] if r['score'] is not None else -1), reverse=True)

    analyzed = counts.get('analyzed', len(results))
    total_long = counts.get('LONG', len(long_results))
    total_short = counts.get('SHORT', len(short_results))
    total_neutral = counts.get('NEUTRAL', len(neutral_results))
    liquidity_excluded = counts.get('liquidity_excluded', 0)

    stale_note = ''
    if stale:
        stale_note = f"⚠️データ収集時刻から{age_hours:.1f}時間経過しています（収集ジョブが失敗した可能性があります）。"

    footer_text = f"データ収集時刻（UTC）：{generated_at}／対象銘柄：{universe_size}"
    if used_fallback:
        footer_text += "（JPX銘柄一覧の取得に失敗したためフォールバック銘柄で実行）"

    # ---- LONG ----
    long_payload = None
    long_csv = None
    if long_results:
        embeds = [format_stock_embed(r) for r in long_results[:10]]
        content = f"🟢 Kurosuke割安チェッカー - ロングシグナル（該当{total_long}銘柄／上位10件を表示）"
        if stale_note:
            content = stale_note + " " + content
        long_payload = {'content': content[:2000], 'embeds': embeds}
        if total_long > 10 or len(long_results) > 10:
            long_csv = build_csv_bytes(long_results, CSV_COLUMNS)

    # ---- SHORT ----
    short_payload = None
    short_csv = None
    if short_results:
        embeds = [format_stock_embed(r) for r in short_results[:10]]
        content = f"🔴 Kurosuke割安チェッカー - ショートシグナル（該当{total_short}銘柄／上位10件を表示）"
        if stale_note:
            content = stale_note + " " + content
        short_payload = {'content': content[:2000], 'embeds': embeds}
        if total_short > 10 or len(short_results) > 10:
            short_csv = build_csv_bytes(short_results, CSV_COLUMNS)

    # ---- WARNING ----
    warning_lines = []
    if stale_note:
        warning_lines.append(f"【データ鮮度】{stale_note}")
    total_failed = len(fund_failed) + len(hist_failed)
    if fund_failed:
        sample = fund_failed[:15]
        warning_lines.append(f"【データ取得エラー（ファンダメンタルズ）】{len(fund_failed)}件")
        warning_lines.extend(f"　{t}：{err}" for t, err in sample)
        if len(fund_failed) > len(sample):
            warning_lines.append(f"　…ほか{len(fund_failed) - len(sample)}件（添付CSV参照）")
    if hist_failed:
        warning_lines.append(f"【データ取得エラー（株価履歴）】{len(hist_failed)}件")

    if analyzed > 0:
        short_ratio = total_short / analyzed
        if short_ratio >= 0.15:
            warning_lines.append(
                f"【市場警戒】分析できた{analyzed}銘柄中{total_short}銘柄"
                f"（{short_ratio * 100:.1f}%）がショートシグナルです。"
            )

    warning_payload = None
    warning_csv = None
    if warning_lines:
        warning_payload = {
            'embeds': [{
                'title': '⚠️ マーケット警告',
                'description': "\n".join(warning_lines)[:4000],
                'color': 0xF39C12,
                'footer': {'text': footer_text},
            }],
        }
        if total_failed > 15:
            warning_csv = build_csv_bytes(
                [{'ticker': t, 'reason': e} for t, e in (fund_failed + hist_failed)],
                ['ticker', 'reason'],
            )

    # ---- PERFORMANCE ----
    perf_embeds = [{
        'title': '📊 本日の分析サマリー',
        'color': 0x3498DB,
        'fields': [
            {'name': '対象銘柄数', 'value': f"{universe_size}", 'inline': True},
            {'name': '分析成功', 'value': f"{analyzed}", 'inline': True},
            {'name': '取得失敗', 'value': f"{total_failed}", 'inline': True},
            {'name': 'ロングシグナル', 'value': f"{total_long}銘柄", 'inline': True},
            {'name': 'ショートシグナル', 'value': f"{total_short}銘柄", 'inline': True},
            {'name': 'ニュートラル', 'value': f"{total_neutral}銘柄", 'inline': True},
            {'name': '流動性フィルタ除外', 'value': f"{liquidity_excluded}銘柄", 'inline': True},
            {'name': 'データ収集時間', 'value': f"{elapsed_minutes}分", 'inline': True},
        ],
        'footer': {'text': footer_text},
    }]

    def _fmt_perf_stats(s):
        if not s or not s.get('closed_count'):
            return '決済済みの仮想取引がまだありません（集計中。数週間ほどお待ちください）'
        parts = [f"決済数：{s['closed_count']}件", f"勝率：{s['win_rate']}%"]
        if s.get('payoff_ratio') is not None:
            parts.append(f"ペイオフレシオ：{s['payoff_ratio']}")
        if s.get('avg_win_pct') is not None:
            parts.append(f"平均利益：+{s['avg_win_pct']}%")
        if s.get('avg_loss_pct') is not None:
            parts.append(f"平均損失：{s['avg_loss_pct']}%")
        return " ／ ".join(parts)

    def _fmt_atr_variants(atr_variants):
        # 【2026-08-26追加、2026-09-11更新】トレーリングストップ幅を変えた場合の
        # 比較。tracking.pyのATR_MULTIPLIER_VARIANTSと表示順を揃える。
        # LONG/SHORTで採用倍率が異なるため（ATR_MULTIPLIER_BY_SIGNAL）、
        # 該当するキーにそれぞれ「現行」ラベルを付ける。
        if not atr_variants:
            return 'データがありません'
        long_key = _variant_key(ATR_MULTIPLIER_BY_SIGNAL['LONG'])
        short_key = _variant_key(ATR_MULTIPLIER_BY_SIGNAL['SHORT'])
        lines = []
        for m in ATR_MULTIPLIER_VARIANTS:
            key = _variant_key(m)
            s = atr_variants.get(key)
            tags = []
            if key == long_key:
                tags.append('LONG現行')
            if key == short_key:
                tags.append('SHORT現行')
            label = f'ATR×{key}' + (f'（{"／".join(tags)}）' if tags else '')
            if not s or not s.get('closed_count'):
                lines.append(f"{label}：決済済みデータがまだありません（集計中）")
                continue
            parts = [f"決済{s['closed_count']}件", f"勝率{s['win_rate']}%"]
            if s.get('payoff_ratio') is not None:
                parts.append(f"ペイオフ{s['payoff_ratio']}")
            lines.append(f"{label}：" + "／".join(parts))
        return "\n".join(lines)

    backtest_embed = None
    if performance_stats:
        backtest_embed = {
            'title': '🎯 シグナル成績（仮想シミュレーション）',
            'description': (
                '毎日のLONG／SHORTシグナルのうち「自信度が高い」上位10銘柄（LONGは複合'
                f'スコア上位、SHORTはPER×PBR上位。それぞれ別枠）にエントリーし、LONGはATR×'
                f'{ATR_MULTIPLIER_BY_SIGNAL["LONG"]}、SHORTはATR×{ATR_MULTIPLIER_BY_SIGNAL["SHORT"]}の'
                'トレーリングストップルールで決済していたと仮定した場合の成績です'
                '（バックテストで倍率がシグナルごとに異なる方が期待値が高いことを'
                '確認し、2026-09-11に変更）。'
                '実際の取引成績ではなく、シグナルそのものの参考成績である点にご注意ください。'
                '\n下部の「ATR倍率比較」は、同じエントリーに対してストップ幅を変えていたら'
                'どうなっていたかの比較です（各倍率が追加された時期より前に開始した'
                'ポジションはその倍率の比較データに含まれません）。'
            ),
            'color': 0x9B59B6,
            'fields': [
                {'name': 'LONG＋SHORT合算', 'value': _fmt_perf_stats(performance_stats.get('long_short')), 'inline': False},
                {'name': 'LONGのみ（上位10銘柄）', 'value': _fmt_perf_stats(performance_stats.get('long_only')), 'inline': False},
                {'name': 'SHORTのみ（上位10銘柄）', 'value': _fmt_perf_stats(performance_stats.get('short_only')), 'inline': False},
                {'name': '現在保有中（未決済）', 'value': f"{performance_stats.get('open_positions', 0)}件", 'inline': True},
                {'name': '🔬 ATR倍率比較（ストップ幅、LONG+SHORT合算）',
                 'value': _fmt_atr_variants(performance_stats.get('atr_variants')), 'inline': False},
            ],
            'footer': {'text': footer_text},
        }
        # パフォーマンスチャンネルにも引き続き表示（ユーザー希望：「今後のためのシグナルなので残しておいて」）
        perf_embeds.append(backtest_embed)

    performance_payload = {'embeds': perf_embeds[:10]}

    # ---- BACKTEST（勝率・ペイオフレシオ専用チャンネル） ----
    backtest_payload = None
    if backtest_embed is not None:
        backtest_payload = {
            'content': '🎯 Kurosuke割安チェッカー - バックテスト結果',
            'embeds': [backtest_embed],
        }

    # ---- STRATEGY ----
    top_long_note = ''
    if long_results:
        top = long_results[0]
        top_long_note = f"本日の最高スコア：{top['ticker']}（{top['name']}）{top['score']:.1f}点"
    note_embed = {
        'description': (
            (stale_note + "\n" if stale_note else '') +
            "本日の判定は、配当利回り・PER×PBR・EPS3期推移（ファンダメンタルズ）に加え、"
            "MA5/25/75・RSI・MACD（テクニカル）、酒田五法パターン検出（10種類）を統合した"
            "複合スコアに基づいています。酒田五法の検出はヒューリスティックによる近似で、"
            "参考精度は引き継ぎ資料記載の数値をそのまま使用（実測のバックテスト値ではありません）。"
            f"\n{top_long_note}"
        ),
        'color': 0x95A5A6,
    }
    strategy_embeds = [note_embed] + [format_stock_embed(r) for r in (long_results[:5] + neutral_results[:4])]
    strategy_payload = {
        'content': "📋 Kurosuke割安チェッカー - 本日の戦略通知",
        'embeds': strategy_embeds[:10],
    }

    return {
        'LONG': (long_payload, long_csv, 'long_candidates.csv'),
        'SHORT': (short_payload, short_csv, 'short_candidates.csv'),
        'WARNING': (warning_payload, warning_csv, 'fetch_errors.csv'),
        'PERFORMANCE': (performance_payload, None, None),
        'STRATEGY': (strategy_payload, None, None),
        'BACKTEST': (backtest_payload, None, None),
    }


# ---------------------------------------------------------------------------
# メイン処理
# ---------------------------------------------------------------------------
def post():
    print("=" * 80)
    print("Kurosuke Discord投稿バッチ（poster.py）")
    print(f"読み込み対象：{SNAPSHOT_PATH}")
    print("=" * 80)

    snapshot, age_hours = load_snapshot()
    if snapshot is None:
        msg = (f"{SNAPSHOT_PATH} が見つかりません。collector.py（データ収集ジョブ）が"
               f"実行・成功しているか確認してください。")
        print(f"[poster] {msg}")
        send_simple_warning(msg)
        return False

    stale = age_hours is not None and age_hours > MAX_SNAPSHOT_AGE_HOURS
    if age_hours is None:
        print("[poster] 収集時刻の解析に失敗したため鮮度チェックをスキップします")
    else:
        print(f"[poster] データ収集からの経過時間：{age_hours:.1f}時間"
              f"（許容：{MAX_SNAPSHOT_AGE_HOURS}時間）")
    if stale:
        print(f"[poster] データが古いため、各チャネルに警告を付記して投稿します")

    payloads = build_payloads(snapshot, stale, age_hours)

    print("\nDiscord に投稿中...")
    send_results = {}
    for channel, (payload, csv_bytes, filename) in payloads.items():
        send_results[channel] = send_discord_message(channel, payload, csv_bytes, filename)

    if all(send_results.values()):
        print("投稿完了！全チャネルへの投稿に成功しました。")
        return True
    else:
        failed_channels = [CHANNEL_LABELS[c] for c, ok in send_results.items() if not ok]
        print(f"投稿処理は完了しましたが、一部チャネルへの投稿に失敗しました：{', '.join(failed_channels)}")
        return False


if __name__ == "__main__":
    try:
        success = post()
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"Error: {str(e)}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
