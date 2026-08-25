#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Kurosuke割安チェッカー - 複数チャネル対応版 + EPS3期推移スコア（Phase 1）

【Phase 0：複数チャネル対応】
- Discord投稿を5種類のチャネル（ロング/ショート/警告/パフォーマンス/戦略通知）に振り分け
- テスト用・本番用のWebhookをそれぞれ独立に設定でき、両方設定されていれば同時投稿
- 新しいWebhook環境変数が1つも設定されていない場合は、従来通り DISCORD_WEBHOOK_URL 1本に
  全メッセージをまとめて送る（後方互換）

【Phase 1：EPS3期推移の追加】
- yfinanceの income_stmt から年次EPSを取得し、直近3期が維持〜増加しているかを判定
- 配当利回り・PER×PBR・EPS3期推移の3項目（EPS取得不可の銘柄は2項目）でスコア表示
- ロング/ショートの振り分け条件そのものは変更せず、従来通り配当利回り・PER×PBRの2条件のまま

【現状の判定ロジックについての注記】
引き継ぎ資料には「酒田五法パターン認識」「21人投資家判定（複合スコアモデル）」等も
判定条件として記載されていましたが、それらはPhase 2・Phase 3以降で実装予定であり、
現時点のコードにはまだ含まれていません。
"""

import os
import sys
import yfinance as yf
from datetime import datetime
import requests

# ---------------------------------------------------------------------------
# Webhook設定
# ---------------------------------------------------------------------------
CHANNELS = ['LONG', 'SHORT', 'WARNING', 'PERFORMANCE', 'STRATEGY']
ENVIRONMENTS = ['TEST', 'PROD']

# チャネル名 → Discordでの表示名（メッセージのヘッダーに使う）
CHANNEL_LABELS = {
    'LONG': 'ロング-シグナル',
    'SHORT': 'ショート-シグナル',
    'WARNING': 'マーケット警告',
    'PERFORMANCE': 'パフォーマンス',
    'STRATEGY': '戦略通知',
}

# GitHub Secrets に登録する名前の例:
#   DISCORD_WEBHOOK_URL_LONG_TEST / _PROD
#   DISCORD_WEBHOOK_URL_SHORT_TEST / _PROD
#   DISCORD_WEBHOOK_URL_WARNING_TEST / _PROD
#   DISCORD_WEBHOOK_URL_PERFORMANCE_TEST / _PROD
#   DISCORD_WEBHOOK_URL_STRATEGY_TEST / _PROD
# ※ 引き継ぎ資料内では5番目が STRATEGY と SAKATA で表記ゆれがあったため、
#   STRATEGY を正式名としつつ SAKATA という名前でも拾えるようにしています。
WEBHOOKS = {}
for _ch in CHANNELS:
    for _env in ENVIRONMENTS:
        WEBHOOKS[(_ch, _env)] = os.getenv(f'DISCORD_WEBHOOK_URL_{_ch}_{_env}')

for _env in ENVIRONMENTS:
    if not WEBHOOKS[('STRATEGY', _env)]:
        WEBHOOKS[('STRATEGY', _env)] = os.getenv(f'DISCORD_WEBHOOK_URL_SAKATA_{_env}')

# 旧バージョンとの後方互換（単一チャネル）
LEGACY_WEBHOOK_URL = os.getenv('DISCORD_WEBHOOK_URL')

# 新しいチャネル別Webhookが1つも設定されていないかどうか
_ANY_NEW_WEBHOOK_SET = any(WEBHOOKS.values())


def get_eps_trend(stock):
    """
    直近の年次EPS（1株当たり利益）を取得し、3期連続で維持〜増加しているかを判定する。

    【注意】yfinanceの income_stmt から 'Diluted EPS' / 'Basic EPS' 行を読み取る実装です。
    yfinanceのバージョンやYahoo! Financeの仕様変更で行名・列名が変わることがあるため、
    取得できない場合は例外を出さずNoneを返します（呼び出し側は「判定不能」として扱い、
    EPS条件をスコアの母数から除外します）。
    このセッションはネットワークが制限されておりYahoo! Financeの実データで動作検証が
    できなかったため、GitHub Actionsでの初回実行時にログ（各銘柄のEPS3期推移）を
    必ず確認してください。
    """
    try:
        stmt = stock.income_stmt
        if stmt is None or stmt.empty:
            return None

        eps_row = None
        for label in ['Diluted EPS', 'Basic EPS']:
            if label in stmt.index:
                eps_row = stmt.loc[label]
                break
        if eps_row is None:
            candidates = [idx for idx in stmt.index if 'eps' in str(idx).lower()]
            if candidates:
                eps_row = stmt.loc[candidates[0]]
        if eps_row is None:
            return None

        eps_row = eps_row.dropna()
        if len(eps_row) < 3:
            return None

        cols_sorted = sorted(eps_row.index)  # 古い期 → 新しい期の順に並べ替え
        values = [float(eps_row[c]) for c in cols_sorted][-3:]
        increasing = all(values[i] <= values[i + 1] for i in range(len(values) - 1))
        return {'values': values, 'increasing': increasing}
    except Exception:
        return None


def get_stock_data(tickers):
    """Yahoo! Finance からデータを取得"""
    print("取得開始...")
    data = {}
    failed = []

    for ticker in tickers:
        try:
            print(f"  取得中：{ticker}...", end=" ")
            stock = yf.Ticker(ticker)
            info = stock.info

            data[ticker] = {
                'name': info.get('longName', 'N/A'),
                'current_price': info.get('currentPrice', 0) or 0,
                'dividend_yield': (info.get('dividendYield', 0) or 0) * 100,
                'pbr': info.get('priceToBook', 0) or 0,
                'per': info.get('trailingPE', 0) or 0,
                'eps_trend': get_eps_trend(stock),
            }
            print("OK")
        except Exception as e:
            print(f"Error: {str(e)}")
            failed.append((ticker, str(e)))

    return data, failed


def classify_stock(data):
    """
    LONG/SHORT/NEUTRALの振り分け条件は従来通り、配当利回り・PER×PBRの2条件のまま
    変更していません（EPS条件をいきなり必須にすると対象銘柄がゼロになりやすいため、
    まずはスコア表示に反映するところから始めています）。

    条件:
      LONG  : 配当利回り 3.5%〜5.8% かつ PER×PBR ≦ 22.5
      SHORT : PER×PBR > 30（割高の目安。要チューニング）
      NEUTRAL: 上記いずれにも該当しない

    スコアは「配当利回り・PER×PBR・EPS3期推移」のうち実際に判定できた項目数を分母にする。
    EPSデータが取得できなかった銘柄は2項目中○個、取得できた銘柄は3項目中○個という表示になる。
    テクニカル分析・酒田五法パターンはPhase 2以降で追加予定のため、現時点のスコアには含めない。
    """
    per = data.get('per', 0)
    pbr = data.get('pbr', 0)
    div = data.get('dividend_yield', 0)
    eps_trend = data.get('eps_trend')
    per_pbr = per * pbr if per and pbr else None

    dividend_ok = 3.5 <= div <= 5.8
    per_pbr_ok = per_pbr is not None and per_pbr <= 22.5
    super_cheap = per_pbr is not None and per_pbr < 15
    eps_ok = eps_trend['increasing'] if eps_trend else None

    score_total = 2 + (1 if eps_trend is not None else 0)
    score_met = int(dividend_ok) + int(per_pbr_ok) + (1 if eps_ok else 0)

    if dividend_ok and per_pbr_ok:
        signal = 'LONG'
    elif per_pbr is not None and per_pbr > 30:
        signal = 'SHORT'
    else:
        signal = 'NEUTRAL'

    return signal, {
        'per_pbr': per_pbr,
        'dividend_ok': dividend_ok,
        'per_pbr_ok': per_pbr_ok,
        'super_cheap': super_cheap,
        'eps_trend': eps_trend,
        'eps_ok': eps_ok,
        'score_met': score_met,
        'score_total': score_total,
    }


def format_stock_line(ticker, data, cond):
    per_pbr_str = f"{cond['per_pbr']:.2f}" if cond['per_pbr'] is not None else "N/A"
    tag = " 🔥超割安" if cond['super_cheap'] else ""

    if cond['eps_trend'] is not None:
        eps_values_str = " → ".join(f"{v:.1f}" for v in cond['eps_trend']['values'])
        eps_status = "増加傾向 ✅" if cond['eps_ok'] else "減少あり ⚠️"
        eps_line = f"  EPS3期推移：{eps_values_str}（{eps_status}）\n"
    else:
        eps_line = "  EPS【データ取得できず】\n"

    return (
        f"{ticker}（{data['name']}）{tag}　判定：{cond['score_met']}/{cond['score_total']}\n"
        f"  株価：¥{data['current_price']:,.0f}\n"
        f"  配当利回り：{data['dividend_yield']:.2f}%\n"
        f"  PER×PBR：{per_pbr_str}\n"
        f"{eps_line}"
    )


def get_webhook_urls(channel):
    """指定チャネルについて、設定されている (環境ラベル, URL) の一覧を返す"""
    urls = []
    for env in ENVIRONMENTS:
        url = WEBHOOKS.get((channel, env))
        if url:
            urls.append((env, url))
    return urls


def send_discord_message(channel, message):
    """
    指定チャネルにDiscordメッセージを送信。
    - 新しいチャネル別Webhookが設定されていればそこへ（TEST/PROD両方設定されていれば両方に送信）
    - 何も設定されておらず、旧 DISCORD_WEBHOOK_URL のみ設定されている場合はそちらに送信
    - どちらも無ければスキップ
    """
    if message is None:
        return True  # 送るべき内容がない（正常）

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
            response = requests.post(url, json={'content': message})
            if response.status_code == 204:
                print(f"[{CHANNEL_LABELS[channel]}/{env_label}] 送信しました")
            else:
                print(f"[{CHANNEL_LABELS[channel]}/{env_label}] 送信失敗：{response.status_code}")
                all_ok = False
        except Exception as e:
            print(f"[{CHANNEL_LABELS[channel]}/{env_label}] Error: {str(e)}")
            all_ok = False

    return all_ok


def build_messages(stock_data, failed_tickers, timestamp):
    """5チャネル分のメッセージを組み立てる（該当なしのチャネルは None）"""
    long_entries, short_entries, neutral_entries = [], [], []

    for ticker, data in stock_data.items():
        signal, cond = classify_stock(data)
        line = format_stock_line(ticker, data, cond)
        if signal == 'LONG':
            long_entries.append(line)
        elif signal == 'SHORT':
            short_entries.append(line)
        else:
            neutral_entries.append(line)

    header = f"実行時刻：{timestamp}\n\n"

    long_message = None
    if long_entries:
        long_message = (
            "🟢 Kurosuke割安チェッカー - ロングシグナル\n" + header + "\n".join(long_entries)
        )

    short_message = None
    if short_entries:
        short_message = (
            "🔴 Kurosuke割安チェッカー - ショートシグナル（割高警戒）\n" + header + "\n".join(short_entries)
        )

    warning_lines = []
    if failed_tickers:
        warning_lines.append("【データ取得エラー】")
        for ticker, err in failed_tickers:
            warning_lines.append(f"  {ticker}：{err}")
    if len(short_entries) >= 3:
        warning_lines.append(f"【市場警戒】対象{len(stock_data)}銘柄中{len(short_entries)}銘柄が割高シグナルです。")
    warning_message = None
    if warning_lines:
        warning_message = "⚠️ Kurosuke割安チェッカー - マーケット警告\n" + header + "\n".join(warning_lines)

    performance_message = (
        "📊 Kurosuke割安チェッカー - 本日のパフォーマンスサマリー\n"
        + header
        + f"分析銘柄数：{len(stock_data)}（取得失敗：{len(failed_tickers)}）\n"
        + f"ロングシグナル：{len(long_entries)}銘柄\n"
        + f"ショートシグナル：{len(short_entries)}銘柄\n"
        + f"ニュートラル：{len(neutral_entries)}銘柄\n"
    )

    strategy_message = (
        "📋 Kurosuke割安チェッカー - 本日の戦略通知\n"
        + header
        + "全銘柄の状況：\n"
        + "".join(neutral_entries + long_entries + short_entries)
        + "\n※現在のスコアは配当利回り・PER×PBR・EPS3期推移の3項目（データ取得可否により2〜3項目）に基づく簡易版です。"
        + "テクニカル分析・酒田五法パターン分析は未実装です（Phase 2で追加予定）。"
    )

    return {
        'LONG': long_message,
        'SHORT': short_message,
        'WARNING': warning_message,
        'PERFORMANCE': performance_message,
        'STRATEGY': strategy_message,
    }


def analyze_stocks():
    """分析実行"""
    tickers = ['6758.T', '7203.T', '9984.T', '6861.T', '8306.T']

    print("=" * 80)
    print("Kurosuke 割安チェッカー - 自動分析（複数チャネル対応版）")
    timestamp = datetime.now().strftime('%Y年%m月%d日 %H:%M:%S')
    print(f"実行時刻：{timestamp}")
    print("=" * 80)

    stock_data, failed_tickers = get_stock_data(tickers)

    if not stock_data:
        print("データ取得に失敗しました")
        send_discord_message(
            'WARNING',
            f"⚠️ Kurosuke割安チェッカー\n実行時刻：{timestamp}\n全銘柄のデータ取得に失敗しました。",
        )
        return False

    messages = build_messages(stock_data, failed_tickers, timestamp)

    print("\nDiscord に投稿中...")
    results = {channel: send_discord_message(channel, msg) for channel, msg in messages.items()}

    if all(results.values()):
        print("分析完了！全チャネルへの投稿に成功しました。")
        return True
    else:
        failed_channels = [CHANNEL_LABELS[c] for c, ok in results.items() if not ok]
        print(f"分析は完了しましたが、一部チャネルへの投稿に失敗しました：{', '.join(failed_channels)}")
        return False


if __name__ == "__main__":
    try:
        success = analyze_stocks()
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"Error: {str(e)}")
        sys.exit(1)
