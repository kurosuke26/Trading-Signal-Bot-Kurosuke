"""
【2026-10-08追加】保有中の仮想ポジションの「売りチェック」をDiscord用に整形する。

これまでDiscordには「保有中N件」しか出ておらず、tracking.py が毎晩計算しているトレーリング
ストップ（損切りライン）を見る手段がなかった。売りの判断（人間の仕事）を空白にしないため、
銘柄ごとに次の4区分で毎朝知らせる。

  🔴 本日手仕舞い … 前日の終値がストップを割った／保有期間の上限に達した → 本日の寄り付きで売る
  ⏳ 本日約定（買い）… 前日のシグナルで建てる → 本日の寄り付きで買う
  🟡 ストップ接近 … 終値とストップの差が NEAR_STOP_PCT（既定3%）以内
  🟢 保有継続 … それ以外

数字はすべて data/trade_log.json（tracking.py の記録）からそのまま取り出す。ここで新しく計算するのは
「ストップまでの距離」「損益率」「保有営業日数」だけ。
"""
import json
import os

import numpy as np

from tracking import LEGACY_BULK_LOAD_ENTRY_DATES, PRIMARY_VARIANT_KEY_BY_SIGNAL, _is_long
from util import env_float

TRADE_LOG_PATH = os.getenv('TRADE_LOG_PATH') or 'data/trade_log.json'
NEAR_STOP_PCT = env_float('NEAR_STOP_PCT', 3.0)

KIND_LABEL = {'LONG': '複合', 'VALUE': '割安', 'GROWTH': '成長', 'FINANCIAL': '金融', 'SHORT': '空売り'}
COLOR = {'exit': 0xE74C3C, 'entry': 0x3498DB, 'near': 0xF1C40F, 'hold': 0x2ECC71}
MAX_DESC = 3900  # Discordのdescription上限4096字に余裕を持たせる


def load_trade_log(path=None):
    path = path or TRADE_LOG_PATH
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _yen(v):
    return f"¥{v:,.0f}" if v is not None else '—'


def _bdays(a, b):
    try:
        return int(np.busday_count(a, b))
    except (TypeError, ValueError):
        return None


def _name(p):
    code = p['ticker'].replace('.T', '')
    return f"{p.get('name') or code}（{code}）"


def classify(trade_log, today_signals=None):
    """
    trade_log を4区分に分ける。戻り値: {'exit': [...], 'entry': [...], 'near': [...], 'hold': [...]}
    各要素は表示用の辞書。today_signals は {ticker: 'LONG'/'SHORT'/...}（当日の判定。警戒表示用）。
    """
    today_signals = today_signals or {}
    out = {'exit': [], 'entry': [], 'near': [], 'hold': []}
    for p in trade_log or []:
        if p.get('entry_date') in LEGACY_BULK_LOAD_ENTRY_DATES:
            continue  # 立ち上げ時の一括分は成績集計と同じく対象外
        sig = p.get('signal')
        key = PRIMARY_VARIANT_KEY_BY_SIGNAL.get(sig)
        if key is None:
            continue
        if p.get('status') == 'pending_entry':
            out['entry'].append({'p': p, 'kind': sig})
            continue
        if p.get('status') != 'open':
            continue
        v = (p.get('variants') or {}).get(key) or {}
        price, stop, entry = p.get('last_price'), v.get('stop'), p.get('entry_price')
        if not entry or price is None:
            continue
        row = {
            'p': p, 'kind': sig, 'price': price, 'stop': stop, 'entry': entry,
            'pnl_pct': ((price - entry) if _is_long(sig) else (entry - price)) / entry * 100,
            'days': _bdays(p.get('entry_date'), p.get('last_price_date')),
            'max_hold': p.get('max_hold_bdays'),
            'caution': _is_long(sig) and today_signals.get(p['ticker']) == 'SHORT',
        }
        if v.get('status') == 'exit_pending':
            row['reason'] = ('保有期間の上限' if v.get('close_reason') == 'max_hold'
                             else f"終値{_yen(v.get('exit_signal_close'))}がストップ{_yen(stop)}を割った")
            out['exit'].append(row)
            continue
        if stop:
            # ストップまでの距離（買いは下、空売りは上）。終値基準
            row['gap_pct'] = ((price - stop) if _is_long(sig) else (stop - price)) / price * 100
        if row.get('gap_pct') is not None and row['gap_pct'] <= NEAR_STOP_PCT:
            out['near'].append(row)
        else:
            out['hold'].append(row)
    out['near'].sort(key=lambda r: r['gap_pct'])
    out['hold'].sort(key=lambda r: r.get('gap_pct') if r.get('gap_pct') is not None else 999)
    return out


def _line(r):
    p = r['p']
    parts = [f"**{_name(p)}**［{KIND_LABEL.get(r['kind'], r['kind'])}］"]
    if 'entry' in r and isinstance(r.get('entry'), (int, float)):
        parts.append(f"取得{_yen(r['entry'])}→終値{_yen(r['price'])}（{r['pnl_pct']:+.1f}%）")
        if r.get('reason'):
            parts.append(r['reason'])
        elif r.get('stop'):
            parts.append(f"ストップ{_yen(r['stop'])}（あと{r['gap_pct']:.1f}%）")
        if r.get('days') is not None:
            parts.append(f"{r['days']}営業日目" + (f"／上限{r['max_hold']}" if r.get('max_hold') else ''))
        if r.get('caution'):
            parts.append('⚠️本日SHORT判定')
    else:  # 約定待ち
        parts.append(f"シグナル日{p.get('signal_date')}の終値{_yen(p.get('signal_price'))}")
    return '・' + ' ／ '.join(parts)


def _embeds_for(title, rows, color):
    """1区分を、description上限に収まるよう必要なら複数のEmbedに分ける。"""
    if not rows:
        return []
    embeds, buf = [], []
    for r in rows:
        line = _line(r)
        if sum(len(x) + 1 for x in buf) + len(line) > MAX_DESC:
            embeds.append(buf)
            buf = []
        buf.append(line)
    embeds.append(buf)
    n = len(embeds)
    return [{'title': title + (f'（{i + 1}/{n}）' if n > 1 else ''), 'description': '\n'.join(b), 'color': color}
            for i, b in enumerate(embeds)]


def build_holdings_payload(trade_log, today_signals=None, as_of=None, stale_note=''):
    """Discordのwebhook payload（content＋embeds）を返す。trade_logが読めなければNone。"""
    if trade_log is None:
        return None
    c = classify(trade_log, today_signals)
    embeds = []
    embeds += _embeds_for(f"🔴 本日の寄り付きで手仕舞い（{len(c['exit'])}件）", c['exit'], COLOR['exit'])
    embeds += _embeds_for(f"⏳ 本日の寄り付きで買い（{len(c['entry'])}件）", c['entry'], COLOR['entry'])
    embeds += _embeds_for(f"🟡 ストップ接近・{NEAR_STOP_PCT:g}%以内（{len(c['near'])}件）", c['near'], COLOR['near'])
    embeds += _embeds_for(f"🟢 保有継続（{len(c['hold'])}件）", c['hold'], COLOR['hold'])
    if not embeds:
        embeds = [{'description': '保有中・約定待ちの仮想ポジションはありません。', 'color': COLOR['hold']}]
    head = (f"🔔 保有ポジションの売りチェック（{as_of}終値時点）" if as_of else "🔔 保有ポジションの売りチェック")
    rule = ("ルール：終値がストップ（終値−ATR×倍率、上げのみ）を割った翌営業日の寄り付きで手仕舞い。"
            "数字は data/trade_log.json（仮想売買の記録）より。実際の売買は各自の判断で。")
    return {'content': (stale_note + "\n" if stale_note else '') + head + "\n" + rule, 'embeds': embeds}
