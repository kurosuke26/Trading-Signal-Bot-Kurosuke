#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tracking.py — LONG/SHORTシグナルの「シグナル通りに売買していたら」の
仮想ポジション追跡・成績集計（勝率・ペイオフレシオ）。

【設計方針】
実際の取引記録ではなく、「その日にbotが出したLONG/SHORTシグナル通りに
エントリーし、scoring.suggested_trade_levels() と同じATR×1.5のトレーリング
ストップルールで決済していたら、どうなっていたか」を collector.py の
日次収集のたびに自動シミュレーションし、data/trade_log.json に蓄積する。

- 同一銘柄・同一方向（LONG/SHORT）で既に建玉中（open）のポジションがあれば、
  新規シグナルが出ても二重にエントリーしない（既存ポジションのトレーリング
  ストップ更新のみ行う）。「建玉中」の判定は下記のATR×1.5（プライマリ）の
  状態のみを見る。
- 決済（close）は、その日の終値がトレーリングストップに抵触したものとして
  扱う（シャンデリア・ストップ方式。LONGは切り上げのみ、SHORTは切り下げの
  み）。この処理は1日1回（deep collectorの実行時）しか行われないため、
  「ストップ価格ちょうどで約定した」のではなく「抵触を検知したその日の
  終値で手仕舞いした」という、日次バッチとしての実態に即した保守的な
  シミュレーションになっている（intraday（日中）の実際のストップ注文約定とは
  異なり、ギャップ（窓開け）が大きい日は損益が理論上のストップ幅より
  大きくぶれ得る）。
- 集計は「LONG＋SHORT合算」「LONGのみ」「SHORTのみ」の3系統を別々に算出する
  （ユーザーの要望：今回の集大成として両方のケースを見たいとのこと）。

【2026-08-26変更：追跡対象を上位10銘柄に絞り込み】
以前はLONG/SHORT判定された銘柄を無条件ですべて追跡していたが、シグナル数が
多いと成績が薄まってしまうため、「自信度が高い」上位TOP_N_TRACKED銘柄
（LONG・SHORTそれぞれ別に選定）だけを新規追跡対象にするよう変更した。
  - LONG: 複合スコア（score）が高い順 = 割安の自信度が高い順
  - SHORT: PER×PBR（per_pbr）が高い順 = 割高度合いが強い順
    （複合スコアはLONG候補の「割安さ」を測るために設計された指標であり、
    SHORT側の確信度としてはper_pbrの方が素直に対応するため）
既に建玉中（open）のポジションは、ランキング圏外に落ちても引き続き
update_open_positions()で決済判定される（絞り込みは「新規に建てるかどうか」
のみに影響し、既存ポジションの追跡は継続する）。

【2026-08-26変更：ATR倍率バリエーションのバックテスト比較を追加】
トレーリングストップの幅（ATRの何倍を損切りラインにするか）を1.5倍・2.0倍・
2.5倍の3パターンで、同じエントリー（同じ銘柄・同じ日・同じ価格・同じATR値）
に対して並行シミュレーションし、勝率・ペイオフレシオを比較できるようにした。
  - 1.5倍（PRIMARY_VARIANT_KEY）＝正式なプライマリ扱い。Discordの
    「エントリー・ストップ目安」欄（scoring.suggested_trade_levels）が
    案内している幅と一致させており、「建玉中かどうか」「新規に追跡対象へ
    入れるかどうか」の判定もこの1.5倍の状態だけを見る。
  - 2.0倍・2.5倍はプライマリと同じエントリーに対する「もしストップ幅を
    広げていたら」の比較用シミュレーションで、1.5倍が決済された後も
    それぞれ自分自身のストップに抵触するまで独立して追跡を続ける。
各ポジションは 'variants' 辞書に倍率ごとの stop/status/close_date/
close_price/return_pct を保持する（1エントリーにつき3バリエーション）。

【正直な注意点：過去データとの互換性】
2026-08-26のこの変更より前に開始したポジションは、エントリー時点のATR値を
保存しておらず、2.0倍・2.5倍のストップを後から正確に再現できない。そのため
_ensure_variants() での読み込み時マイグレーションでは、既存ポジションは
1.5倍（プライマリ）のみを引き継ぎ、2.0倍・2.5倍は 'not_tracked'（比較対象
外）として扱う。この変更以降に新規開設されたポジションから、3倍率すべての
比較データが蓄積されていく（統計として意味のある比較ができるまでには数週間
〜数ヶ月かかる見込み）。

【正直な注意点：シミュレーションそのものについて】
これは実際の売買記録ではなく、シグナル通りに機械的に売買した場合の
シミュレーションです。実際にどの銘柄を選んで取引するかはユーザー次第
であり、この勝率・ペイオフレシオはあくまで「シグナルそのものの成績」の
目安であって、ユーザー個人の実績ではありません。また、収集開始直後は
決済済みの取引が無いため「集計中」と表示されます。統計として意味のある
件数が貯まるまでには数週間程度かかる見込みです。

【2026-09-11変更：プライマリ倍率をLONG/SHORTで分離】
J-Quantsバックテスト（全3,735銘柄・482営業日、1.5〜6.0倍で比較）の結果、
LONGは4.5〜5.0倍で期待値がピークに達し6.0倍で低下すること、SHORTは1.8倍で
明確にピークを打つことを確認した（詳細: Claude outputs/の分析メモ）。
これによりATR_MULTIPLIER（旧: 両シグナル共通1.5倍）を廃止し、
ATR_MULTIPLIER_BY_SIGNAL（LONG=5.0倍／SHORT=1.8倍）に分離した。上記の
「1.5倍がプライマリ」等の記述は歴史的経緯として残しているが、現在の
プライマリ判定はATR_MULTIPLIER_BY_SIGNAL／PRIMARY_VARIANT_KEY_BY_SIGNAL
（シグナルごとに参照）で行う。なお業種別の最適化は、1業種あたりの
サンプル数不足（16〜128件）のため見送っている。

Discord投稿の「エントリー・ストップ目安」欄（scoring.suggested_trade_levels、
discord-ai-team側の_format_candidate）は、本変更時点ではまだATR×1.5固定の
ままで、この変更を反映していない（表示の見直しは別途要検討・要合意）。
"""

import json
import os

from util import env_int, json_default

TRADE_LOG_PATH = os.getenv('TRADE_LOG_PATH') or 'data/trade_log.json'
FALLBACK_STOP_PCT = 0.03  # ATRが算出できない銘柄向けの簡易フォールバック（±3%相当）
TOP_N_TRACKED = env_int('TOP_N_TRACKED', 10)  # 新規追跡対象とする「自信度上位」銘柄数（SHORTに適用）

# 【2026-09-11追加】LONGは無条件の上位10銘柄エントリーをやめ、複合スコアによる
# 「買いシグナル」判定に切り替える。バックテストでの検証結果はtracking.pyの
# _select_top_candidates()docstring参照。
LONG_ENTRY_SCORE_THRESHOLD = env_int('LONG_ENTRY_SCORE_THRESHOLD', 90)
LONG_TOP_N = env_int('LONG_TOP_N', 5)  # 該当銘柄が多い日でも上位5件までに絞る

# 【2026-08-26追加】トレーリングストップの倍率バリエーション（比較バックテスト用）。
# 1.5倍がプライマリ（Discordの「エントリー・ストップ目安」欄と一致させる正式な幅）。
# 複数の倍率を扱うため、他の実行パラメータと違って環境変数では変更できない
# （変更したい場合はこのリスト自体を編集する）。
#
# 【2026-09-06追加】J-Quantsバックテスト(backtest.py)でLONG/SHORTとも1.5/2.0/2.5倍を
# 比較した結果、LONGは倍率を上げるほど勝率・期待値とも改善する一方、SHORTは2.0倍付近が
# ピークで2.5倍にすると急激に悪化するという非対称な傾向が判明した。そこで、SHORTの
# 最適値をピンポイントで特定するため1.5〜2.5の間を細かく刻み、LONGは改善傾向がどこで
# 頭打ちになるか確認するため2.5より先も伸ばして、1回の再実行でまとめて比較できるよう
# 候補を追加した。
#
# 【2026-09-07追加】上記の再実行で、SHORTは1.8倍でピーク（期待値+0.77%）を打ち
# 2.7倍以降は期待値がマイナスに転落することが判明。一方LONGは3.5倍まで見ても
# まだ期待値が伸び続けており頭打ちが確認できなかったため、真の最適値を探すべく
# さらに広い倍率（4.0〜6.0）を追加する。
#
# 【2026-09-11追加：本番採用】4.0〜6.0倍を含めた完全な再バックテスト（全3,735銘柄・
# 482営業日・重複記録バグ修正後のクリーンな結果）で、LONGは4.5〜5.0倍で期待値が
# ピーク（+5.07%/+5.08%）に達し6.0倍で低下に転じること、SHORTは1.8倍でピーク
# （+0.77%）のまま変わらないことを確認した。これにより、LONG/SHORTで異なる
# プライマリ倍率を採用する（ATR_MULTIPLIER_BY_SIGNAL参照）。
# なお業種別の内訳も分析したが、1業種あたりの決済件数が16〜128件と少なく
# 「最適倍率」が業種ごとに1.8〜6.0倍までばらつくなど、ノイズの域を出ない
# （J-Quants Freeプランのデータ期間制約＝2年3ヶ月では時期尚早と判断し、
# 業種別の倍率分けは見送る）。
ATR_MULTIPLIER = 1.5  # 過去形式ポジションのマイグレーション専用（下記_ensure_variants参照）
ATR_MULTIPLIER_BY_SIGNAL = {'LONG': 5.0, 'SHORT': 1.8}  # 本番のプライマリ倍率（シグナル別）
ATR_MULTIPLIER_VARIANTS = [1.5, 1.8, 2.0, 2.2, 2.5, 2.7, 3.0, 3.5, 4.0, 4.5, 5.0, 6.0]


def _variant_key(multiplier):
    return f'{multiplier:.1f}'


# 2026-08-26以前の旧形式ポジション（'variants'辞書を持たない）をマイグレーションする際、
# 唯一保持していた倍率がどのキーだったかを示す（_ensure_variants参照）。本番の新規判定には使わない。
LEGACY_PRIMARY_VARIANT_KEY = _variant_key(ATR_MULTIPLIER)
# 【2026-09-11追加】シグナル別の本番プライマリ倍率キー。「建玉中かどうか」「決済判定」は
# このキーで行う（_has_open/update_open_positions/compute_performance_stats参照）。
PRIMARY_VARIANT_KEY_BY_SIGNAL = {sig: _variant_key(m) for sig, m in ATR_MULTIPLIER_BY_SIGNAL.items()}


def load_trade_log(path=None):
    """
    【注意】write_snapshot/load_snapshotと同様、デフォルト引数を
    path=TRADE_LOG_PATH とすると関数定義時点の値に固定されてしまうため、
    path=None にして呼び出し時にモジュールグローバルを参照する形にしている。
    """
    if path is None:
        path = TRADE_LOG_PATH
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        trade_log = data if isinstance(data, list) else []
    except Exception:
        # 壊れたファイルで全処理が止まらないよう、空リストから再出発する
        return []

    # 2026-08-26のATR倍率バリエーション追加より前の旧形式ポジションを、
    # 新形式（'variants'辞書を持つ形式）へ読み込み時に変換する。
    for p in trade_log:
        _ensure_variants(p)
    return trade_log


def save_trade_log(trade_log, path=None):
    if path is None:
        path = TRADE_LOG_PATH
    out_dir = os.path.dirname(path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    tmp_path = path + '.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(trade_log, f, ensure_ascii=False, default=json_default)
    os.replace(tmp_path, path)


def _ensure_variants(p):
    """
    (a) 2026-08-26のATR倍率バリエーション追加より前の旧形式ポジション
    （'variants'キーが無く、フラットな'stop'/'close_date'等でATR×1.5のみを
    保持していた形式）を、新形式（倍率ごとに'variants'辞書で保持する形式）に
    その場（in-place）で変換する。旧形式はエントリー時点のATR値を保存していない
    ため、初期ストップを後から再現できる倍率が無く、レガシープライマリ（1.5倍）
    のみを引き継ぐ。

    (b) ATR_MULTIPLIER_VARIANTSは運用中に随時拡張されてきた
    （2026-08-26: 1.5/2.0/2.5 → 2026-09-07: 〜3.5 → 2026-09-11: 〜6.0）ため、
    'variants'辞書は既にあっても、拡張後に追加された倍率のキーが無いポジションが
    ある。entry_price・atr_at_entryが分かっていれば、その時点で建てていたと
    みなして初期ストップを計算し'open'として追跡を始める（無ければ'not_tracked'）。

    どちらのケースも何度呼んでも安全（冪等）。
    """
    if 'variants' not in p:
        p['variants'] = {
            LEGACY_PRIMARY_VARIANT_KEY: {
                'stop': p.pop('stop', None),
                'status': p.get('status', 'open'),
                'close_date': p.pop('close_date', None),
                'close_price': p.pop('close_price', None),
                'return_pct': p.pop('return_pct', None),
            },
        }

    entry_price = p.get('entry_price')
    atr_at_entry = p.get('atr_at_entry')
    signal = p.get('signal')
    for m in ATR_MULTIPLIER_VARIANTS:
        key = _variant_key(m)
        if key in p['variants']:
            continue
        if entry_price and signal:
            stop = _initial_stop(entry_price, atr_at_entry, signal, m)
            p['variants'][key] = {
                'stop': round(stop, 2), 'status': 'open',
                'close_date': None, 'close_price': None, 'return_pct': None,
            }
        else:
            p['variants'][key] = {
                'stop': None, 'status': 'not_tracked',
                'close_date': None, 'close_price': None, 'return_pct': None,
            }
    return p


def _has_open(trade_log, ticker, signal):
    """
    「建玉中かどうか」はプライマリ（シグナル別。PRIMARY_VARIANT_KEY_BY_SIGNAL参照）
    の状態だけで判定する（他の倍率は比較用の並行シミュレーションであり、
    新規追跡対象に入れるかどうかの判定には使わない）。
    """
    return any(p['ticker'] == ticker and p['signal'] == signal and p['status'] == 'open' for p in trade_log)


def _initial_stop(entry_price, atr_value, signal, multiplier):
    if atr_value is None or atr_value <= 0:
        atr_value = entry_price * FALLBACK_STOP_PCT
    if signal == 'LONG':
        return entry_price - atr_value * multiplier
    return entry_price + atr_value * multiplier  # SHORT


def _select_top_candidates(results, signal, top_n=TOP_N_TRACKED):
    """
    その日の判定結果から、指定したシグナル（LONG/SHORT）のうち「自信度が高い」
    上位top_n銘柄だけを選ぶ。基準はモジュールdocstring【2026-08-26変更】を参照。

    【2026-09-11追加】LONGは無条件のtop_n選定をやめ、複合スコアが
    LONG_ENTRY_SCORE_THRESHOLD以上の銘柄だけを候補にする（該当が無ければ0件でよい）。
    バックテストで、スコア90点以上に絞ると勝率50.6%→64.5%・ペイオフ2.23→2.20
    （件数454、全体の約3割）に改善することを確認した（詳細: Claude outputs/の分析メモ）。
    SHORTは同様の絞り込み条件を探索したが、勝率・ペイオフを両方改善できるものが
    サンプル数100件以上では見つからなかったため、無条件top_nのまま変更しない。
    """
    candidates = [r for r in results.values() if r.get('signal') == signal]
    if signal == 'LONG':
        candidates = [r for r in candidates if (r.get('score') if r.get('score') is not None else -1) >= LONG_ENTRY_SCORE_THRESHOLD]
        candidates.sort(key=lambda r: (r.get('score') if r.get('score') is not None else -1), reverse=True)
    else:  # SHORT
        candidates.sort(key=lambda r: (r.get('per_pbr') if r.get('per_pbr') is not None else 0), reverse=True)
    return candidates[:top_n]


def open_new_positions(trade_log, results, today_str, top_n=TOP_N_TRACKED):
    """
    今回の収集でLONG/SHORT判定になった銘柄のうち、「自信度が高い」候補だけを
    対象に、まだ建玉中(open)のものが無い銘柄について新規の仮想ポジションを1件開く。
    戻り値は新規開設件数。
    LONG: 複合スコアがLONG_ENTRY_SCORE_THRESHOLD以上の銘柄のみ、上位LONG_TOP_N件まで
    （該当が無い日は0件でよい。無条件top_n選定は廃止）。
    SHORT: 引き続き無条件でPER×PBR上位top_n件。

    1件のポジションにつき、同じエントリー価格・同じATR値に対してATR_MULTIPLIER_VARIANTS
    の全倍率のストップを同時に設定する。「建玉中かどうか」「決済判定」は
    シグナル別のプライマリ倍率（ATR_MULTIPLIER_BY_SIGNAL）のみで行う。
    """
    opened = 0
    top_candidates = _select_top_candidates(results, 'LONG', LONG_TOP_N) + _select_top_candidates(results, 'SHORT', top_n)
    for r in top_candidates:
        ticker = r['ticker']
        signal = r.get('signal')
        if _has_open(trade_log, ticker, signal):
            continue
        entry_price = r.get('current_price')
        if not entry_price or entry_price <= 0:
            continue
        atr_value = (r.get('tech_snapshot') or {}).get('atr')

        variants = {}
        for m in ATR_MULTIPLIER_VARIANTS:
            stop = _initial_stop(entry_price, atr_value, signal, m)
            variants[_variant_key(m)] = {
                'stop': round(stop, 2),
                'status': 'open',
                'close_date': None,
                'close_price': None,
                'return_pct': None,
            }

        trade_log.append({
            'ticker': ticker,
            'name': r.get('name'),
            'signal': signal,
            'entry_date': today_str,
            'entry_price': round(entry_price, 2),
            'atr_at_entry': round(atr_value, 4) if atr_value else None,
            # 【2026-09-07追加】ATR倍率の業種別最適化分析（backtest.py参照）用に、
            # エントリー時点の業種コードを記録しておく。fundamental_snapshotが
            # 無い（Phase2未対応データ）場合はNoneのまま。
            'sector_code': (r.get('fundamental_snapshot') or {}).get('sector_code'),
            'sector_name': (r.get('fundamental_snapshot') or {}).get('sector_name'),
            'status': 'open',  # プライマリ（シグナル別。ATR_MULTIPLIER_BY_SIGNAL）が決済されるまで'open'
            'variants': variants,
        })
        opened += 1
    return opened


def update_open_positions(trade_log, results, today_str):
    """
    建玉中の全ポジションについて、ATR_MULTIPLIER_VARIANTSの倍率ごとに、今回取得
    できた最新価格・ATRでトレーリングストップを更新し、抵触していれば決済
    （close）する。プライマリ倍率（シグナル別。ATR_MULTIPLIER_BY_SIGNAL）が
    先に決済されても、他の倍率はそれぞれ自分自身のストップに抵触するまで独立して
    追跡を継続する（'not_tracked'のバリエーションは対象外のまま）。

    今回データ取得に失敗した銘柄（resultsに存在しない）は判定をスキップし、
    次回の収集時にあらためて判定する（データ欠損による誤決済を避けるため）。

    戻り値は「プライマリ」が決済された件数（従来の意味と同じ。
    collector.pyの実行ログ表示に使われる）。
    """
    closed_primary = 0
    for p in trade_log:
        variants = p.get('variants') or {}
        if not any(v.get('status') == 'open' for v in variants.values()):
            continue

        r = results.get(p['ticker'])
        current_price = r.get('current_price') if r else None
        raw_atr = (r.get('tech_snapshot') or {}).get('atr') if r else None
        if r is None or not current_price or current_price <= 0:
            continue  # 今回データ欠損。全バリエーションとも次回まで持ち越す

        for m in ATR_MULTIPLIER_VARIANTS:
            key = _variant_key(m)
            v = variants.get(key)
            if not v or v.get('status') != 'open':
                continue

            atr_value = raw_atr
            if atr_value is None or atr_value <= 0:
                atr_value = current_price * FALLBACK_STOP_PCT

            if p['signal'] == 'LONG':
                candidate_stop = current_price - atr_value * m
                new_stop = max(v['stop'], candidate_stop) if v.get('stop') is not None else candidate_stop
                hit = current_price <= new_stop
            else:  # SHORT
                candidate_stop = current_price + atr_value * m
                new_stop = min(v['stop'], candidate_stop) if v.get('stop') is not None else candidate_stop
                hit = current_price >= new_stop

            v['stop'] = round(new_stop, 2)

            if hit:
                entry = p['entry_price']
                if p['signal'] == 'LONG':
                    ret_pct = (current_price - entry) / entry * 100
                else:
                    ret_pct = (entry - current_price) / entry * 100
                v['status'] = 'closed'
                v['close_date'] = today_str
                v['close_price'] = round(current_price, 2)
                v['return_pct'] = round(ret_pct, 2)
                if key == PRIMARY_VARIANT_KEY_BY_SIGNAL.get(p['signal']):
                    p['status'] = 'closed'
                    closed_primary += 1
    return closed_primary


def _stats_for(closed_trades):
    n = len(closed_trades)
    if n == 0:
        return {
            'closed_count': 0, 'win_count': 0, 'win_rate': None,
            'avg_win_pct': None, 'avg_loss_pct': None, 'payoff_ratio': None,
        }
    wins = [t['return_pct'] for t in closed_trades if t['return_pct'] is not None and t['return_pct'] > 0]
    losses = [t['return_pct'] for t in closed_trades if t['return_pct'] is not None and t['return_pct'] <= 0]
    win_rate = round(len(wins) / n * 100, 1)
    avg_win = round(sum(wins) / len(wins), 2) if wins else None
    avg_loss = round(sum(losses) / len(losses), 2) if losses else None
    payoff = None
    if avg_win is not None and avg_loss is not None and avg_loss != 0:
        payoff = round(avg_win / abs(avg_loss), 2)
    return {
        'closed_count': n, 'win_count': len(wins), 'win_rate': win_rate,
        'avg_win_pct': avg_win, 'avg_loss_pct': avg_loss, 'payoff_ratio': payoff,
    }


def _closed_variant_trades(trade_log, variant_key, signal=None):
    """指定した倍率バリエーション（variant_key）について、決済済みのものだけを集める。"""
    out = []
    for p in trade_log:
        if signal is not None and p.get('signal') != signal:
            continue
        v = (p.get('variants') or {}).get(variant_key)
        if v and v.get('status') == 'closed':
            out.append(v)
    return out


def compute_performance_stats(trade_log):
    """
    勝率・ペイオフレシオを算出する。

    - 'long_short' / 'long_only' / 'short_only'：プライマリ（シグナル別。
      ATR_MULTIPLIER_BY_SIGNAL＝LONG5.0倍／SHORT1.8倍）のトレーリングストップ
      での決済結果を「LONG＋SHORT合算」「LONGのみ」「SHORTのみ」の3系統で
      集計したもの。合算はLONG側のプライマリ決済とSHORT側のプライマリ決済を
      単純に足し合わせる（倍率が異なる決済同士を混ぜる形になる点に注意）。
    - 'atr_variants'：ATR_MULTIPLIER_VARIANTSの各倍率で決済していたと仮定した
      場合の比較集計（LONG＋SHORT合算）。倍率が拡張される前に開始した
      ポジションはその倍率のデータが無いため、比較対象には含まれない点に
      注意（_ensure_variants()参照）。
    """
    closed_primary_long = _closed_variant_trades(trade_log, PRIMARY_VARIANT_KEY_BY_SIGNAL['LONG'], 'LONG')
    closed_primary_short = _closed_variant_trades(trade_log, PRIMARY_VARIANT_KEY_BY_SIGNAL['SHORT'], 'SHORT')
    closed_primary_all = closed_primary_long + closed_primary_short
    open_count = len([p for p in trade_log if p.get('status') == 'open'])

    atr_variants = {
        _variant_key(m): _stats_for(_closed_variant_trades(trade_log, _variant_key(m)))
        for m in ATR_MULTIPLIER_VARIANTS
    }

    return {
        'long_short': _stats_for(closed_primary_all),
        'long_only': _stats_for(closed_primary_long),
        'short_only': _stats_for(closed_primary_short),
        'open_positions': open_count,
        'atr_variants': atr_variants,
    }
