#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
value_growth_scoring.py — 銘柄の種類ごとに分けたスコア（2026-10-08追加）。

  VALUE    割安（金融業を除く）
  GROWTH   成長（金融業を除く）
  FINANCIAL 金融（銀行・証券・保険・その他金融。PER×PBRや現金比率が他業種と同じ物差しで測れないため別枠）
  ROMAN    ロマン枠（10倍株の候補。監視のみで仮想売買はしない）

【背景】
2026-10-08の検証（パネル：約3,900銘柄・2024-07〜2026-06、同日の取引可能銘柄内の順位で5分位比較）で、
  - 効いた：PBRの低さ・予想PERの低さ・ネットキャッシュ（現金÷時価総額）
            ／成長株は「割安さ」または「売買代金の増加・52週高値への近さ」と組み合わせた場合のみ
  - 効かない・逆効果：売上・利益の伸び単独、会社予想の成長率単独、ROE・ROC単独、配当利回り単独、
            業績予想の上方修正・決算進捗率
という結果が出た。そこで「割安」と「成長」を別のスコアに分け、成長は「足切り（入口の条件）」にだけ使い、
順位付けは割安さと需給・トレンドで行う。

【割安（VALUE）スコア 0〜100】
  入口の条件：PER×PBR ≦ 22.5（kurosuke割安チェッカーの根幹。グレアムの基準）・金融業以外
  配点（VALUE_WEIGHTS。PER・PBR系に偏りすぎないよう、業種内の比較・現金・財務・勢いに分散。2026-10-08見直し）：
    PER×PBR（絶対水準）   25点  ≦14で満点、14〜22.5は 0.6→0.2 に直線で減点（kurosukeさんの指定：≦14をより高く）
    業種内の割安さ       20点  PER×PBRの同業種内の順位（低いほど高得点。同じ日・同じ業種の銘柄が5社以上の場合）
    ネットキャッシュ     20点  現金÷時価総額の高さの順位
    配当利回り          15点  3.5%以上で満点（7%超は減配リスクを見て0.6）、2.5〜3.5%は0.6、1.5〜2.5%は0.3
    財務の健全性         10点  自己資本比率の順位
    業種内の勢い         10点  60日リターンの「同業種平均との差」の順位
  ※見直し前（PER×PBR35・PBR20・PER15・現金15・配当15）は70点がPER・PBR系で、実質この2つで順位が決まっていた。

【成長（GROWTH）スコア 0〜100】
  入口の条件（成長の足切り。点数にはしない）：
    直近決算の売上が前年同期比+10%以上 かつ 営業利益が前年同期比+10%以上
    かつ 会社予想の営業利益が前期比で減益でない（予想が無ければ問わない）かつ PERがプラス・金融業以外
  配点：割安さ（50点）PEGの低さ25点＋PERの低さ25点／需給・トレンド（50点）売買代金の増加・52週高値への近さ・120日リターンを各1/3

【金融（FINANCIAL）スコア 0〜100】（銀行・証券・保険・その他金融）
  入口の条件：PBR・ROEがプラス
  順位は金融業の中だけで付ける。
    PBR÷ROE（稼ぐ力の割に安いか）30点／PBRの低さ 20点／ROEの高さ 20点／配当利回り 20点／業種内の勢い 10点

【ロマン枠（ROMAN）0〜100】（10倍株の候補。監視のみ）
  入口の条件：時価総額50〜1,500億円・直近決算の売上と営業利益が前年同期比+15%以上・会社予想の営業利益+10%以上・
             予想PER 0〜30倍・ROE 8%以上（取れる場合）・金融業以外
  配点（Yartseva 2025「小型・割安・高収益」とMayer『100倍株』「資本利益率と再投資」を参考に）：
    予想の利益成長 20点／売上の伸び 15点／ROE 20点／EV/EBIT利回り（割安さ）15点／時価総額の小ささ 10点／52週高値への近さ 20点
  10倍になるには何年もかかり、手元の約2年のデータでは検証できないため、仮想売買はせず一覧の記録だけを残す。
  【検証結果（evaluate_roman.py）】この条件（記事・論文ベース、v1）は、その後半年で2倍に達した割合が0.8%と、
  同じ時価総額帯の全銘柄（4.7%）より低かった（すでに評価された成長株を拾ってしまう）。本番の一覧には使わない。

【ロマン枠v2（ROMAN2）】本番の一覧はこちら
  前半期間だけで「その後半年で2倍になった銘柄が事前に持っていた特徴」を調べて決めた条件。
  入口：時価総額50〜1,500億円・金融業以外・会社予想の営業利益が期初予想から上方修正されている
  配点：時価総額の小ささ30点／120日の上昇40点／業種内の勢い30点
  後半（確かめる期間）の精度：入口を通った全銘柄で半年以内に2倍到達6.9%（基準5.4%、1.27倍）・1.5倍到達21.5%（基準17.2%）。
  上位10件に絞ると後半は2倍到達3.8%と基準を下回り、順位付けの効果は確認できなかった。

【データ欠損】
項目が欠けている場合は、その項目を除いた配点で100点満点に引き直す（scoring.composite_scoreと同じ考え方）。
ただし判定に使える配点が半分未満の銘柄はスコアをNaNにする（データ不足で順位がぶれるのを防ぐ）。

本モジュールは pandas.DataFrame（1行＝1銘柄、同じ日の銘柄の集まり）を受け取って列を足して返すだけで、
データ取得は行わない。検証（evaluate_value_growth.py）と本番（collector.py）の両方から同じ関数を使う。
"""

import numpy as np
import pandas as pd

PER_PBR_MAX = 22.5
PER_PBR_SUPER = 14.0
MIN_SECTOR_SIZE = 5  # 業種内の比較は、同じ日・同じ業種の銘柄がこの数以上ある場合だけ

VALUE_WEIGHTS = {'per_pbr': 25, 'sec_per_pbr': 20, 'cash': 20, 'div': 15, 'equity': 10, 'sec_mom': 10}
VALUE_WEIGHTS_OLD = {'per_pbr': 35, 'pbr': 20, 'per': 15, 'cash': 15, 'div': 15}  # 2026-10-08 見直し前（比較用）
GROWTH_WEIGHTS = {'peg': 25, 'per': 25, 'flow': 50 / 3, 'high52': 50 / 3, 'ret120': 50 / 3}
FINANCIAL_WEIGHTS = {'pbr_roe': 30, 'pbr': 20, 'roe': 20, 'div': 20, 'sec_mom': 10}
ROMAN_WEIGHTS = {'op_fc': 20, 'sales': 15, 'roe': 20, 'ey': 15, 'small': 10, 'high52': 20}
ROMAN2_WEIGHTS = {'small': 30, 'ret120': 40, 'sec_mom': 30}

FINANCIAL_SECTORS = {'銀行業', '証券、商品先物取引業', '保険業', 'その他金融業'}

GROWTH_MIN_SALES_YOY = 0.10
GROWTH_MIN_OP_YOY = 0.10
ROMAN_MKTCAP_OKU = (50, 1500)
ROMAN_MIN_YOY = 0.15
ROMAN_MIN_OP_FORECAST = 0.10
ROMAN_MAX_PER_FORECAST = 30
ROMAN_MIN_ROE = 0.08
MIN_AVAILABLE_WEIGHT = 50

# 入力として使う列（無い列はNaN扱い）
INPUT_COLUMNS = ('per', 'pbr', 'per_forecast', 'div_yield', 'cash_to_mktcap', 'sales_yoy', 'op_yoy',
                 'op_growth_forecast', 'eps_growth_forecast', 'sales_growth_forecast', 'value_ratio_20_60',
                 'high_52w_ratio', 'ret_120', 'ret_60', 'equity_ratio', 'roe', 'mktcap_oku', 'earnings_yield_ev',
                 'op_revision_from_initial')


def per_pbr_points(x):
    """PER×PBR → 0〜1。≦14で1.0、14〜22.5で0.6→0.2、22.5超は0。"""
    if x is None or not np.isfinite(x) or x <= 0:
        return np.nan
    if x <= PER_PBR_SUPER:
        return 1.0
    if x <= PER_PBR_MAX:
        return 0.6 - 0.4 * (x - PER_PBR_SUPER) / (PER_PBR_MAX - PER_PBR_SUPER)
    return 0.0


def dividend_points(y):
    """配当利回り（%）→ 0〜1。"""
    if y is None or not np.isfinite(y):
        return np.nan
    if y > 7.0:
        return 0.6
    if y >= 3.5:
        return 1.0
    if y >= 2.5:
        return 0.6
    if y >= 1.5:
        return 0.3
    return 0.0


def _rank(s, ascending=True):
    """0〜1の順位。ascending=True なら値が大きいほど1に近い。欠損はNaNのまま。"""
    s = pd.to_numeric(s, errors='coerce').replace([np.inf, -np.inf], np.nan)
    r = s.rank(pct=True)
    return r if ascending else 1 - r + 1 / max(int(s.notna().sum()), 1)


def _sector_rank(s, sector, ascending=True):
    """同じ業種の中での順位（0〜1）。業種の銘柄数がMIN_SECTOR_SIZE未満ならNaN。"""
    s = pd.to_numeric(s, errors='coerce').replace([np.inf, -np.inf], np.nan)
    g = s.groupby(sector)
    r = g.rank(pct=True)
    n = g.transform('count')
    r = r if ascending else 1 - r + 1 / n.clip(lower=1)
    return r.where(n >= MIN_SECTOR_SIZE)


def weighted_score(parts, weights):
    """parts: name -> Series(0〜1 or NaN)。欠損は配点から除いて100点満点に引き直す。"""
    num = sum(parts[k].fillna(0) * w for k, w in weights.items())
    den = sum(parts[k].notna().astype(float) * w for k, w in weights.items())
    score = num / den.replace(0, np.nan) * 100
    return score.where(den >= MIN_AVAILABLE_WEIGHT)


def _parts_frame(df, prefix):
    return {c[len(prefix):]: df[c] for c in df.columns if c.startswith(prefix)}


def rescore(df, kind, weights):
    """add_scores 済みのdfについて、配点だけを変えたスコアを返す（検証用）。"""
    return weighted_score(_parts_frame(df, f'{kind}_parts_'), weights)


def add_scores(df):
    """
    df に以下の列を追加して返す（元のdfは変更しない）:
      value_gate/value_score, growth_gate/growth_score, financial_gate/financial_score, roman_gate/roman_score,
      peg, per_pbr_calc, is_financial, sec_per_pbr_ratio（PER×PBR÷同業種中央値）, sec_ret60_diff（60日リターン−同業種平均）
      *_parts_*（内訳。0〜1）
    """
    df = df.copy()
    for c in INPUT_COLUMNS:
        if c not in df.columns:
            df[c] = np.nan
        df[c] = pd.to_numeric(df[c], errors='coerce').replace([np.inf, -np.inf], np.nan)
    if 'sector_name' not in df.columns:
        df['sector_name'] = None
    sector = df['sector_name'].fillna('（業種不明）')
    is_fin = df['sector_name'].isin(FINANCIAL_SECTORS)
    df['is_financial'] = is_fin

    per = df['per'].where(df['per'] > 0)
    pbr = df['pbr'].where(df['pbr'] > 0)
    per_pbr = per * pbr
    df['per_pbr_calc'] = per_pbr

    # 業種内の比較（表示にも使う）
    sec_size = sector.map(sector.value_counts())
    df['sec_per_pbr_ratio'] = (per_pbr / per_pbr.groupby(sector).transform('median')).where(sec_size >= MIN_SECTOR_SIZE)
    df['sec_ret60_diff'] = (df['ret_60'] - df['ret_60'].groupby(sector).transform('mean')).where(sec_size >= MIN_SECTOR_SIZE)

    # ---- VALUE（金融業を除く）----
    # 銀行・証券・保険などの現金は預金・顧客資産で、会社の余剰資金ではないため、ネットキャッシュの採点から外す
    cash = df['cash_to_mktcap'].where((df['cash_to_mktcap'] >= 0) & ~is_fin)
    vp = {
        'per_pbr': per_pbr.map(per_pbr_points),
        'sec_per_pbr': _sector_rank(per_pbr, sector, ascending=False),
        'pbr': _rank(pbr, ascending=False),
        'per': _rank(per, ascending=False),
        'cash': _rank(cash),
        'div': df['div_yield'].map(dividend_points),
        'equity': _rank(df['equity_ratio'].where(~is_fin)),
        'sec_mom': _rank(df['sec_ret60_diff']),
    }
    for k, v in vp.items():
        df[f'value_parts_{k}'] = v
    df['value_score'] = weighted_score(vp, VALUE_WEIGHTS)
    df['value_gate'] = per_pbr.notna() & (per_pbr <= PER_PBR_MAX) & ~is_fin

    # ---- GROWTH（金融業を除く）----
    eg = df['eps_growth_forecast']
    pf = df['per_forecast'].where(df['per_forecast'] > 0)
    df['peg'] = (pf / (eg * 100)).where((eg > 0) & pf.notna())
    gp = {
        'peg': _rank(df['peg'], ascending=False),
        'per': _rank(per, ascending=False),
        'flow': _rank(df['value_ratio_20_60']),
        'high52': _rank(df['high_52w_ratio']),
        'ret120': _rank(df['ret_120']),
    }
    for k, v in gp.items():
        df[f'growth_parts_{k}'] = v
    df['growth_score'] = weighted_score(gp, GROWTH_WEIGHTS)
    ogf = df['op_growth_forecast']
    df['growth_gate'] = ((df['sales_yoy'] >= GROWTH_MIN_SALES_YOY) & (df['op_yoy'] >= GROWTH_MIN_OP_YOY)
                         & (ogf.isna() | (ogf >= 0)) & per.notna() & ~is_fin)

    # ---- FINANCIAL（金融業の中だけで順位）----
    roe = df['roe']
    fin_pbr = pbr.where(is_fin)
    fin_roe = roe.where(is_fin & (roe > 0))
    fp = {
        'pbr_roe': _rank(fin_pbr / fin_roe, ascending=False),
        'pbr': _rank(fin_pbr, ascending=False),
        'roe': _rank(fin_roe),
        'div': df['div_yield'].where(is_fin).map(dividend_points),
        'sec_mom': _rank(df['sec_ret60_diff'].where(is_fin)),
    }
    for k, v in fp.items():
        df[f'financial_parts_{k}'] = v
    df['financial_score'] = weighted_score(fp, FINANCIAL_WEIGHTS)
    df['financial_gate'] = is_fin & fin_pbr.notna() & fin_roe.notna()

    # ---- ROMAN（10倍株の候補・監視のみ）----
    mc = df['mktcap_oku']
    rp = {
        'op_fc': _rank(ogf),
        'sales': _rank(df['sales_yoy']),
        'roe': _rank(roe.where(~is_fin)),
        'ey': _rank(df['earnings_yield_ev']),
        'small': _rank(mc, ascending=False),
        'high52': _rank(df['high_52w_ratio']),
    }
    for k, v in rp.items():
        df[f'roman_parts_{k}'] = v
    df['roman_score'] = weighted_score(rp, ROMAN_WEIGHTS)
    df['roman_gate'] = ((mc >= ROMAN_MKTCAP_OKU[0]) & (mc <= ROMAN_MKTCAP_OKU[1])
                        & (df['sales_yoy'] >= ROMAN_MIN_YOY) & (df['op_yoy'] >= ROMAN_MIN_YOY)
                        & (ogf >= ROMAN_MIN_OP_FORECAST) & pf.notna() & (pf <= ROMAN_MAX_PER_FORECAST)
                        & (roe.isna() | (roe >= ROMAN_MIN_ROE)) & ~is_fin)

    # ---- ROMAN v2（その後大きく上がった銘柄が事前に持っていた特徴から作った条件）----
    in_band = (mc >= ROMAN_MKTCAP_OKU[0]) & (mc <= ROMAN_MKTCAP_OKU[1]) & ~is_fin
    r2 = {
        'small': _rank(mc.where(in_band), ascending=False),
        'ret120': _rank(df['ret_120'].where(in_band)),
        'sec_mom': _rank(df['sec_ret60_diff'].where(in_band)),
    }
    for k, v in r2.items():
        df[f'roman2_parts_{k}'] = v
    df['roman2_score'] = weighted_score(r2, ROMAN2_WEIGHTS)
    df['roman2_gate'] = in_band & (df['op_revision_from_initial'] > 0)
    return df


# ---------------------------------------------------------------------------
# 本番（collector.py）用：その日の判定結果にスコアを付ける
# ---------------------------------------------------------------------------
VG_KEEP_COLUMNS = ('value_score', 'value_gate', 'growth_score', 'growth_gate', 'financial_score', 'financial_gate',
                   'roman_score', 'roman_gate', 'roman2_score', 'roman2_gate', 'op_revision_from_initial', 'is_financial',
                   'avg_value_20', 'above_ma50', 'per_pbr_calc', 'peg',
                   'sec_per_pbr_ratio', 'sec_ret60_diff', 'cash_to_mktcap', 'equity_ratio', 'roe', 'mktcap_oku',
                   'sales_yoy', 'op_yoy', 'op_growth_forecast', 'per_forecast', 'value_ratio_20_60',
                   'high_52w_ratio', 'ret_120')


def price_features(df):
    """株価履歴（yfinanceの日足、分割・配当調整済み）から、build_panel.py と同じ定義の値を出す。
    本番の株価履歴は約9か月分なので、52週高値は取れる範囲（最大250営業日）の高値で代用する。"""
    out = {'ret_60': None, 'ret_120': None, 'high_52w_ratio': None, 'above_ma50': None, 'value_ratio_20_60': None,
           'avg_value_20': None}
    if df is None or getattr(df, 'empty', True) or 'Close' not in df:
        return out
    c = pd.to_numeric(df['Close'], errors='coerce').dropna()
    if len(c) < 2:
        return out
    last = float(c.iloc[-1])
    if len(c) > 60 and c.iloc[-61] > 0:
        out['ret_60'] = last / float(c.iloc[-61]) - 1
    if len(c) > 120 and c.iloc[-121] > 0:
        out['ret_120'] = last / float(c.iloc[-121]) - 1
    hi = float(c.iloc[-250:].max())
    out['high_52w_ratio'] = last / hi if hi > 0 else None
    if len(c) >= 50:
        out['above_ma50'] = bool(last > float(c.iloc[-50:].mean()))
    if 'Volume' in df and len(df) >= 80:
        val = (pd.to_numeric(df['Close'], errors='coerce') * pd.to_numeric(df['Volume'], errors='coerce'))
        v20, v60 = val.iloc[-20:].mean(), val.iloc[-80:-20].mean()
        out['avg_value_20'] = float(v20) if v20 == v20 else None
        if v60 and v60 > 0 and v20 == v20:
            out['value_ratio_20_60'] = float(v20 / v60)
    return out


def jquants_features(ticker, current_price, market_cap, today_str):
    """J-Quantsキャッシュ（fins.json）から、今日時点の成長率・予想・現金比率などを出す（追加の通信なし）。
    分割は開示時点の株数のまま扱う（直近の開示後に分割があった銘柄は1株あたり値がずれるが、
    ここで使うのは成長率・比率・予想PERのみで、予想PERだけが影響を受ける）。"""
    from fundamental_features import FundamentalState, load_fins_records
    records = load_fins_records(ticker.replace('.T', ''))
    if not records:
        return {}
    st = FundamentalState(lambda d: 1.0)
    for rec in records:
        if rec['DiscDate'] <= today_str:
            st.apply(rec)
    mcap_mil = market_cap / 1e6 if market_cap else None
    return st.features(today_str, 1.0, current_price, mcap_mil)


def annotate(results, histories, today_str):
    """
    collector.py の results（ticker -> 判定結果dict）の各銘柄に r['vg'] を付ける。
    順位は「その日に判定できた全銘柄」の中で計算する。戻り値は付けた銘柄数。
    """
    rows = []
    for t, r in results.items():
        fs = r.get('fundamental_snapshot') or {}
        jq = jquants_features(t, r.get('current_price'), r.get('market_cap'), today_str)
        px = price_features((histories or {}).get(t))
        rows.append({
            'ticker': t,
            'per': r.get('per'), 'pbr': r.get('pbr'), 'div_yield': r.get('dividend_yield') or None,
            'mktcap_oku': (r['market_cap'] / 1e8) if r.get('market_cap') else jq.get('mktcap_oku'),
            'sector_name': fs.get('sector_name') or r.get('sector_name'), **px,
            **{k: jq.get(k) for k in ('per_forecast', 'cash_to_mktcap', 'sales_yoy', 'op_yoy', 'op_growth_forecast',
                                      'eps_growth_forecast', 'sales_growth_forecast', 'equity_ratio', 'roe',
                                      'earnings_yield_ev', 'op_revision_from_initial')},
        })
    if not rows:
        return 0
    scored = add_scores(pd.DataFrame(rows)).set_index('ticker')
    for t, row in scored.iterrows():
        vg = {}
        for k in VG_KEEP_COLUMNS:
            v = row.get(k)
            if isinstance(v, (bool, np.bool_)):
                vg[k] = bool(v)
            elif v is None or (isinstance(v, float) and not np.isfinite(v)):
                vg[k] = None
            else:
                vg[k] = round(float(v), 4) if isinstance(v, (int, float, np.floating, np.integer)) else v
        results[t]['vg'] = vg
    return len(scored)


def _band(score, labels):
    if score is None or score != score:
        return '判定不能'
    for thr, lab in labels:
        if score >= thr:
            return lab
    return '中立'


def value_band_label(score):
    return _band(score, ((80, '割安度が極めて高い'), (70, '割安度が高い'), (60, 'やや割安')))


def growth_band_label(score):
    return _band(score, ((80, '割安な成長株・勢い強い'), (70, '成長株として有望'), (60, 'やや有望')))


def financial_band_label(score):
    return _band(score, ((80, '稼ぐ力の割に極めて割安'), (70, '稼ぐ力の割に割安'), (60, 'やや割安')))


def roman_band_label(score):
    return _band(score, ((80, '有力候補'), (70, '候補'), (60, '監視')))
