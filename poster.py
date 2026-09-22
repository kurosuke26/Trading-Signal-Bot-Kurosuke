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

import sector_relative
from scoring import score_band_label, suggested_trade_levels
from tracking import (
    ATR_MULTIPLIER_BY_SIGNAL, ATR_MULTIPLIER_VARIANTS, _variant_key,
    LONG_ENTRY_SCORE_THRESHOLD, LONG_TOP_N, LONG_REQUIRE_SECTOR_MOMENTUM,
)
from util import env_float, json_default

SNAPSHOT_PATH = os.getenv('SCAN_OUTPUT_PATH') or 'data/latest_scan.json'
MAX_SNAPSHOT_AGE_HOURS = env_float('MAX_SNAPSHOT_AGE_HOURS', 12)

# ---------------------------------------------------------------------------
# Webhook設定（従来から変更なし）
# ---------------------------------------------------------------------------
CHANNELS = ['LONG', 'SHORT', 'WARNING', 'PERFORMANCE', 'STRATEGY', 'BACKTEST', 'TIPS', 'BREAKING']
ENVIRONMENTS = ['TEST', 'PROD']

CHANNEL_LABELS = {
    'LONG': 'ロング-シグナル',
    'SHORT': 'ショート-シグナル',
    'WARNING': 'マーケット警告',
    'PERFORMANCE': 'パフォーマンス',
    'STRATEGY': '戦略通知',
    'BACKTEST': 'バックテスト結果',  # 勝率・ペイオフレシオ（tracking.py）専用チャンネル
    'TIPS': '週次tips',              # weekly_tips.py がここに投稿（poster.py本体は使わない）
    'BREAKING': '速報',              # breaking_alerts.py がここに投稿（poster.py本体は使わない）
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
    score = r.get('score')
    # 【2026-09-11追加】LONGで複合スコアがLONG_ENTRY_SCORE_THRESHOLD以上＝
    # 「買いシグナル点灯（実際に仮想エントリーしてトレーリングストップをかける対象）」
    # であることを、日々のロング一覧の中でも目立つようにする。
    over_threshold = r.get('signal') == 'LONG' and score is not None and score >= LONG_ENTRY_SCORE_THRESHOLD
    # 【2026-09-22修正】仮想エントリーは「75点以上」かつ「業種内モメンタム（60日リターンが同業種平均以上）」。
    # 75点以上でも業種内モメンタムを満たさない銘柄を「実際に仮想エントリー中」と表示していたのを直す。
    sector_ok = sector_relative.passes(r, require_momentum=LONG_REQUIRE_SECTOR_MOMENTUM)
    is_buy_signal = over_threshold and sector_ok
    title_prefix = ('🎯買いシグナル点灯！ ' if is_buy_signal else
                    '⏸ ' if over_threshold else ('🔥 ' if r.get('super_cheap') else ''))
    title = f"{title_prefix}{r['ticker']}（{r.get('name')}）"
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

    sector_line = sector_relative.format_line(r)
    if sector_line:
        fields.append({'name': '業種内の位置', 'value': sector_line, 'inline': False})

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

    description = SIGNAL_LABEL.get(r.get('signal'), r.get('signal'))
    color = SIGNAL_COLOR.get(r.get('signal'), 0x95A5A6)
    if is_buy_signal:
        description = (f"🎯 買いシグナル点灯（複合スコア{LONG_ENTRY_SCORE_THRESHOLD}点以上＋業種内モメンタム）／"
                        f"仮想エントリーの対象（同じ銘柄を保有中の場合は重複して建てません）")
        color = 0xF1C40F  # 金色：通常のLONG（緑）と区別して目立たせる
    elif over_threshold:
        description = (f"⏸ 複合スコア{LONG_ENTRY_SCORE_THRESHOLD}点以上ですが、60日リターンが同業種平均を下回るため"
                       f"仮想エントリーの対象外（業種内モメンタム待ち）")
        color = 0xE67E22

    return {
        'title': title,
        'description': description,
        'color': color,
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

# 【2026-09-16追加】ロング通知に並べる最低スコア。LONG_ENTRY_SCORE_THRESHOLD（75）以上が仮想エントリー対象で、
# この点数〜75点未満は「参考」として境界線の下に並べる。
LONG_DISPLAY_MIN_SCORE = float(os.getenv('LONG_DISPLAY_MIN_SCORE', '70'))
LONG_CSV_COLUMNS = ['band', 'ticker', 'name', 'score', 'current_price', 'dividend_yield', 'per_pbr', 'entry_timing']


def _score_of(r):
    return r['score'] if r.get('score') is not None else -1


def build_long_payload(long_results, total_long, stale_note=''):
    """
    ロング通知：複合スコアLONG_DISPLAY_MIN_SCORE点以上の銘柄をスコアの高い順に並べ、
    LONG_ENTRY_SCORE_THRESHOLD点（仮想エントリー対象）との境に線を入れる。
    Discordの1投稿あたりEmbedは10件までのため、入りきらない分は添付CSV（区分列つき）で全件を渡す。
    戻り値: (payload, csv_bytes or None)
    """
    shown = [r for r in long_results if _score_of(r) >= LONG_DISPLAY_MIN_SCORE]
    over = [r for r in shown if _score_of(r) >= LONG_ENTRY_SCORE_THRESHOLD]
    # 75点以上でも業種内モメンタムを満たさない銘柄は、線より上に「⏸対象外」として並べる（スコア順は維持）
    buy = list(over)
    n_entry = sum(1 for r in over if sector_relative.passes(r, require_momentum=LONG_REQUIRE_SECTOR_MOMENTUM))
    watch = [r for r in shown if _score_of(r) < LONG_ENTRY_SCORE_THRESHOLD]
    lo, hi = f'{LONG_DISPLAY_MIN_SCORE:g}', f'{LONG_ENTRY_SCORE_THRESHOLD:g}'
    if not shown:
        content = (f"🟢 Kurosuke割安チェッカー - ロングシグナル（複合スコア{lo}点以上の銘柄は本日なし／"
                   f"LONG判定{total_long}銘柄）")
        return {'content': ((stale_note + ' ') if stale_note else '') + content}, None

    embeds = [format_stock_embed(r) for r in buy[:10]]
    if watch and len(embeds) < 9:
        embeds.append({'description': f"──────── ここから下は {lo}〜{hi}点未満（参考・仮想エントリー対象外） ────────",
                       'color': 0x7F8C8D})
        embeds += [format_stock_embed(r) for r in watch[:10 - len(embeds)]]
    shown_in_embeds = min(len(buy), 10) + (min(len(watch), max(0, 10 - min(len(buy), 10) - 1)) if watch and len(buy) < 9 else 0)
    content = (f"🟢 Kurosuke割安チェッカー - ロングシグナル（複合スコア{lo}点以上{len(shown)}銘柄をスコアの高い順に表示）\n"
               f"🎯 {hi}点以上：{len(buy)}銘柄（うち業種内モメンタムも満たす仮想エントリー対象 {n_entry}銘柄）"
               f" ／ 👀 {lo}〜{hi}点未満（参考）：{len(watch)}銘柄")
    if shown_in_embeds < len(shown):
        content += f"\n※表示しきれない{len(shown) - shown_in_embeds}銘柄を含む全件は添付CSVを参照（band列で区分）"
    if stale_note:
        content = stale_note + ' ' + content
    csv_bytes = None
    if shown_in_embeds < len(shown):
        rows = [dict(r, band=(f'{hi}点以上（仮想エントリー対象）' if sector_relative.passes(r, require_momentum=LONG_REQUIRE_SECTOR_MOMENTUM)
                              else f'{hi}点以上（業種内モメンタム待ち・対象外）')) for r in buy] + \
               [dict(r, band=f'{lo}〜{hi}点未満（参考）') for r in watch]
        csv_bytes = build_csv_bytes(rows, LONG_CSV_COLUMNS)
    return {'content': content[:2000], 'embeds': embeds[:10]}, csv_bytes


def _fmt_num(v, suffix='%', signed=True):
    if v is None:
        return '—'
    return f"{v:+.2f}{suffix}" if signed else f"{v}{suffix}"


def build_event_embeds(event_summary):
    """別枠のイベント型仮想売買（event_strategies.py）の投稿用Embed。戻り値: dict(channel -> [embed])"""
    out = {'STRATEGY': [], 'WARNING': [], 'PERFORMANCE': []}
    if not event_summary:
        return out
    hikes = event_summary.get('new_dividend_hikes') or []
    if hikes:
        lines = [f"・{p['ticker']}（{p.get('name') or ''}）{(p.get('reason') or '')[:40]}" for p in hikes[:15]]
        if len(hikes) > 15:
            lines.append(f"…ほか{len(hikes) - 15}件")
        out['STRATEGY'].append({
            'title': f"💴 増配修正の発表（別枠の仮想売買）：本日{len(hikes)}件",
            'description': ("TDnetで「（増配）」を明示した配当予想の修正・剰余金の配当を検知。翌営業日の始値で仮想エントリーし、"
                            "終値−ATR×3.0のトレーリングで管理します（本番LONGとは別の検証用の系列）。\n" + "\n".join(lines))[:4000],
            'color': 0x27AE60,
        })
    crash = event_summary.get('crash')
    if crash:
        cands = crash.get('candidates') or []
        lines = [f"・{p['ticker']}：20日高値から{p['drop_pct']}%" for p in cands[:20]]
        if len(cands) > 20:
            lines.append(f"…ほか{len(cands) - 20}銘柄")
        out['WARNING'].append({
            'title': f"🚨 暴落時の行動ルール発動（{crash['date']}）",
            'description': (f"取引可能な銘柄の等金額指数が20営業日高値から{crash['index_dd_pct']}%。"
                            f"20日高値から−20%以上下げた取引可能な{len(cands)}銘柄が対象です。\n"
                            "ルール：翌営業日の始値で分割して買い、利確ATR×4／損切りATR×2／最長20営業日。"
                            "過去約2年で暴落は3回のみのため、一度に資金を集中させない前提の目安です。\n"
                            + "\n".join(lines))[:4000],
            'color': 0xC0392B,
        })
    perf = event_summary.get('performance') or {}
    if perf:
        fields = []
        for v in perf.values():
            fields.append({'name': v['label'],
                           'value': (f"決済済み{v['closed']}件／保有中{v['open']}件／約定待ち{v['pending_entry']}件\n"
                                     f"勝率{_fmt_num(v['win_rate_pct'], '%', False)}・ペイオフ{_fmt_num(v['payoff_ratio'], '', False)}・"
                                     f"期待値{_fmt_num(v['expectancy_pct'])}・市場平均との差{_fmt_num(v['excess_pct'])}"),
                           'inline': False})
        idx = event_summary.get('index') or {}
        out['PERFORMANCE'].append({
            'title': '🧪 別枠の仮想売買（実運用での検証中）',
            'description': (f"指数（取引可能な銘柄の等金額平均）の20日高値からの位置：{_fmt_num(idx.get('dd20_pct'))}"
                            f"（{idx.get('date', '—')}時点、−10%以下で暴落ルール発動）"),
            'fields': fields, 'color': 0x8E44AD,
        })
    return out


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


# 【2026-09-16追加】Discordの制限：1投稿あたりEmbedは10件まで、かつ全Embedの合計6,000文字まで。
# 件数だけ守って文字数を超えると、Discordは400ではなく500を返す（2026-09-16にロング通知10件・8,071文字で発生）。
MAX_EMBEDS_PER_MESSAGE = 10
MAX_EMBED_TOTAL_CHARS = 5800  # 6,000の手前で余裕を持たせる
# 【2026-09-16】実測：8フィールドのEmbedを10件（合計80フィールド）送ると500エラー、9件（72）なら成功。
# 公式には明記が無いが、1投稿あたりのフィールド総数にも上限があるとみられるため余裕を持って72で止める。
MAX_EMBED_TOTAL_FIELDS = 72


def _embed_chars(embed):
    """Discordが数える文字数（title/description/footer/author/fieldのname・value）。"""
    n = len(embed.get('title') or '') + len(embed.get('description') or '')
    n += len((embed.get('footer') or {}).get('text') or '')
    n += len((embed.get('author') or {}).get('name') or '')
    for f in embed.get('fields') or []:
        n += len(f.get('name') or '') + len(f.get('value') or '')
    return n


def limit_embeds(embeds):
    """Discordの上限内に収まるようEmbedを絞る。戻り値: (収まったEmbed, 落とした件数)"""
    kept, total, fields = [], 0, 0
    for e in embeds or []:
        c, f = _embed_chars(e), len(e.get('fields') or [])
        if (len(kept) >= MAX_EMBEDS_PER_MESSAGE or total + c > MAX_EMBED_TOTAL_CHARS
                or fields + f > MAX_EMBED_TOTAL_FIELDS):
            break
        kept.append(e)
        total += c
        fields += f
    return kept, len(embeds or []) - len(kept)


def send_discord_message(channel, payload, file_bytes=None, filename=None):
    if payload is None:
        return True

    if payload.get('embeds'):
        kept, dropped = limit_embeds(payload['embeds'])
        if dropped:
            payload = dict(payload, embeds=kept)
            note = f"（表示は{len(kept)}件まで。残り{dropped}件はDiscordの表示上限のため省略しています）"
            payload['content'] = ((payload.get('content') or '') + chr(10) + note)[:2000]
            print(f"[{CHANNEL_LABELS[channel]}] Embedが上限を超えたため{dropped}件を省略しました")

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
    # 【2026-09-11変更、同日再変更】ロングチャンネルは原則どおり日々の上位候補を
    # 一覧表示し続ける（該当が無い日でも一覧自体は投稿する）。そのうち複合スコアが
    # LONG_ENTRY_SCORE_THRESHOLD以上の銘柄だけが「実際に仮想エントリーしトレーリング
    # ストップをかける買いシグナル」であり、format_stock_embed()側で金色・
    # 🎯マーク付きの見た目にして一覧の中で目立たせる（バックテストでの効果は
    # Claude outputs/2026-09-11-long-entry-score-gate.md参照）。
    # 【2026-09-16変更】複合スコア70点以上をスコアの高い順に並べ、75点（仮想エントリー対象）との境に線を入れる
    long_payload, long_csv = build_long_payload(long_results, total_long, stale_note)
    long_buy_signals = [r for r in long_results if _score_of(r) >= LONG_ENTRY_SCORE_THRESHOLD
                        and sector_relative.passes(r, require_momentum=LONG_REQUIRE_SECTOR_MOMENTUM)]
    event_embeds = build_event_embeds(snapshot.get('event_strategies'))

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
    # 【2026-09-22修正】株価の上限フィルター（MAX_SHARE_PRICE）による除外はエラーではないので分けて表示する
    price_excluded = [(t, e) for t, e in fund_failed if '以上のため除外' in str(e)]
    fund_failed = [(t, e) for t, e in fund_failed if '以上のため除外' not in str(e)]
    if price_excluded:
        warning_lines.append(f"【株価の上限フィルターで除外】{len(price_excluded)}銘柄（{price_excluded[0][1]}）："
                             + '、'.join(t for t, _ in price_excluded[:20]) + ('…' if len(price_excluded) > 20 else ''))
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
    if warning_lines or event_embeds['WARNING']:
        embeds = list(event_embeds['WARNING'])
        if warning_lines:
            embeds.append({
                'title': '⚠️ マーケット警告',
                'description': "\n".join(warning_lines)[:4000],
                'color': 0xF39C12,
                'footer': {'text': footer_text},
            })
        warning_payload = {'embeds': embeds[:10]}
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
            {'name': f'うち買いシグナル点灯(≧{LONG_ENTRY_SCORE_THRESHOLD}点＋業種内モメンタム)',
             'value': f"{len(long_buy_signals)}銘柄", 'inline': True},
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
                f'LONGは複合スコア{LONG_ENTRY_SCORE_THRESHOLD}点以上かつ60日リターンが同業種平均以上の上位{LONG_TOP_N}銘柄、'
                'SHORTはPER×PBR上位10銘柄に毎日エントリーし、LONGはATR×'
                f'{ATR_MULTIPLIER_BY_SIGNAL["LONG"]}、SHORTはATR×{ATR_MULTIPLIER_BY_SIGNAL["SHORT"]}の'
                'トレーリングストップで決済していたと仮定した場合の成績です'
                '（2026-09-16に先読みなしの検証に基づき条件を更新。立ち上げ時2026-08-25の一括分は集計対象外）。'
                '実際の取引成績ではなく、シグナルそのものの参考成績である点にご注意ください。'
                '\n下部の「ATR倍率比較」は、同じエントリーに対してストップ幅を変えていたら'
                'どうなっていたかの比較です（各倍率が追加された時期より前に開始した'
                'ポジションはその倍率の比較データに含まれません）。'
            ),
            'color': 0x9B59B6,
            'fields': [
                {'name': 'LONG＋SHORT合算', 'value': _fmt_perf_stats(performance_stats.get('long_short')), 'inline': False},
                {'name': f'LONGのみ（{LONG_ENTRY_SCORE_THRESHOLD}点以上＋業種内モメンタム・上位{LONG_TOP_N}）', 'value': _fmt_perf_stats(performance_stats.get('long_only')), 'inline': False},
                {'name': 'SHORTのみ（PER×PBR上位10）', 'value': _fmt_perf_stats(performance_stats.get('short_only')), 'inline': False},
                {'name': '現在保有中（未決済）', 'value': f"{performance_stats.get('open_positions', 0)}件", 'inline': True},
                {'name': '🔬 ATR倍率比較（ストップ幅、LONG+SHORT合算）',
                 'value': _fmt_atr_variants(performance_stats.get('atr_variants')), 'inline': False},
            ],
            'footer': {'text': footer_text},
        }
        # パフォーマンスチャンネルにも引き続き表示（ユーザー希望：「今後のためのシグナルなので残しておいて」）
        perf_embeds.append(backtest_embed)

    perf_embeds.extend(event_embeds['PERFORMANCE'])
    performance_payload = {'embeds': perf_embeds[:10]}

    # ---- BACKTEST（勝率・ペイオフレシオ専用チャンネル） ----
    backtest_payload = None
    if backtest_embed is not None:
        backtest_payload = {
            'content': '🎯 Kurosuke割安チェッカー - バックテスト結果',
            'embeds': [backtest_embed] + event_embeds['PERFORMANCE'],
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
            "複合スコアに基づいています。酒田五法は教科書どおりの定義で「形が完成した日」だけを検出し、"
            "表示の実測精度は過去約2年・全銘柄で、検出翌日の始値から20営業日後に当たる向きへ動いた割合です"
            "（将来の成績を保証するものではありません）。"
            f"\n{top_long_note}"
        ),
        'color': 0x95A5A6,
    }
    strategy_embeds = [note_embed] + event_embeds['STRATEGY'] + \
        [format_stock_embed(r) for r in (long_results[:5] + neutral_results[:4])]
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
