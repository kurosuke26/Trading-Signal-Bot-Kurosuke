#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
investor_models.py — 投資家タイプ別のスクリーニング条件とスコア（2026-09-16追加）。

有名投資家が書籍・インタビュー等で公開している銘柄選びの考え方を、J-Quants無料データ（株価・約2年分の
決算短信・会社予想・予想修正）で測れる条件に置き換えたもの。原典の条件のうち、データが無くて測れない
もの（20年連続配当、10年間のEPS成長、流動比率、株主優待の内容、機関投資家の保有動向など）は
「代替条件」または「省略」として明記している。

【呼び方のルール】
discord-ai-team の投資判断レンズの方針（実在の投資家個人の名前を投稿で使わない）に合わせ、
Discord等に出す名前は style_label（「〇〇派」）を使う。investor は条件の出典を示すための社内メモ。

【先読み】
条件はすべて build_panel.py の特徴量（t日の引けまでに分かる値）だけを使う。fwd_ で始まる列は使わない。
同じ日の銘柄間の順位（percentile）は、その日に取引可能な銘柄の中だけで計算する。

各条件は DataFrame（パネル）を受け取り、1.0（満たす）/0.0（満たさない）/NaN（データ不足で判定不能）の
Series を返す。core=True の条件をすべて満たす銘柄が「合格」。スコアは判定可能な条件の重み付き充足率（0〜100）。
"""

import numpy as np
import pandas as pd


def _b(cond, *cols_needed):
    """真偽の Series を 1.0/0.0 にし、必要な列が欠けている行は NaN にする。"""
    out = cond.astype(float)
    for c in cols_needed:
        out = out.where(c.notna())
    return out


def add_cross_sectional_ranks(df):
    """同じ日の取引可能な銘柄の中での順位（0〜1、大きいほど値が大きい）を追加する。"""
    df = df.copy()
    g = df.groupby('date')
    for col in ('mom_12_1', 'ret_120', 'high_52w_ratio', 'div_yield_forecast', 'per_actual', 'per_forecast', 'pbr',
                'mktcap_oku', 'earnings_yield_ev', 'roc'):
        df[f'rank_{col}'] = g[col].rank(pct=True)
    df['mktcap_order'] = g['mktcap_oku'].rank(ascending=False, method='first')
    # 市場全体の地合い：その日、50日線より上にある銘柄の割合
    df['market_breadth_50'] = g['above_ma50'].transform(lambda s: s.astype(float).mean())
    # 大型株100社の中での配当利回り順位（ダウの犬の日本版）
    big = df['mktcap_order'] <= 100
    df['rank_div_in_top100'] = np.nan
    df.loc[big, 'rank_div_in_top100'] = df[big].groupby('date')['div_yield_forecast'].rank(ascending=False,
                                                                                            method='first')
    df['peg'] = np.where((df['eps_growth_forecast'] > 0) & (df['per_forecast'] > 0),
                         df['per_forecast'] / (df['eps_growth_forecast'] * 100), np.nan)
    df['total_return_ratio'] = np.where(df['per_forecast'] > 0,
                                        (df['eps_growth_forecast'] * 100 + df['div_yield_forecast']) / df['per_forecast'],
                                        np.nan)
    df['greenblatt_rank'] = (df['rank_earnings_yield_ev'] + df['rank_roc']) / 2
    return df


def C(label, fn, weight=1.0, core=True):
    return {'label': label, 'fn': fn, 'weight': weight, 'core': core}


MODELS = [
    # =====================================================================
    # 高配当・インカム
    # =====================================================================
    {'type': '高配当', 'investor': 'ベンジャミン・グレアム（防衛的投資家の銘柄選択基準）', 'style_label': '防衛的バリュー配当派',
     'summary': '大型・黒字・継続配当で、PER×PBR≦22.5（株価が利益と純資産の両面で割高でない）。kurosuke割安チェッカーの22.5の出典。',
     'omitted': '20年連続配当・10年間の利益成長・流動比率2倍以上（データ期間外／項目なし）→ 自己資本比率40%以上で代替',
     'conditions': [
         C('PER×PBR≦22.5', lambda d: _b(d.per_actual * d.pbr <= 22.5, d.per_actual, d.pbr)),
         C('PER≦15', lambda d: _b(d.per_actual <= 15, d.per_actual)),
         C('配当あり（前期実績）', lambda d: _b(d.div_yield_actual > 0, d.div_yield_actual)),
         C('時価総額1,000億円以上', lambda d: _b(d.mktcap_oku >= 1000, d.mktcap_oku)),
         C('営業CFが黒字', lambda d: _b(d.cfo_positive == True, d.cfo_positive)),  # noqa: E712
         C('自己資本比率40%以上', lambda d: _b(d.equity_ratio >= 0.4, d.equity_ratio), core=False),
     ]},
    {'type': '高配当', 'investor': 'ジョン・ネフ（トータルリターン比率）', 'style_label': '低PER・トータルリターン派',
     'summary': '（予想EPS成長率＋配当利回り）÷PER が高い＝成長と配当の割に株価が安い銘柄。成長率は7〜20%程度の「ほどほど」を好む。',
     'omitted': '業種の成熟度の定性判断 → 省略',
     'conditions': [
         C('トータルリターン比率1.5以上', lambda d: _b(d.total_return_ratio >= 1.5, d.total_return_ratio)),
         C('予想PERが市場の下位40%', lambda d: _b(d.rank_per_forecast <= 0.4, d.rank_per_forecast)),
         C('予想EPS成長率5〜25%', lambda d: _b(d.eps_growth_forecast.between(0.05, 0.25), d.eps_growth_forecast)),
         C('予想配当利回り2%以上', lambda d: _b(d.div_yield_forecast >= 2, d.div_yield_forecast), core=False),
     ]},
    {'type': '高配当', 'investor': 'デビッド・ドレマン（逆張り投資）', 'style_label': '逆張り高配当派',
     'summary': 'PER・PBRが市場の下位20%で配当利回りが高い、嫌われた中大型株。財務の健全性で割安トラップを避ける。',
     'omitted': '流動比率 → 自己資本比率で代替',
     'conditions': [
         C('PERが市場の下位20%', lambda d: _b(d.rank_per_actual <= 0.2, d.rank_per_actual)),
         C('PBRが市場の下位20%', lambda d: _b(d.rank_pbr <= 0.2, d.rank_pbr)),
         C('予想配当利回りが市場の上位30%', lambda d: _b(d.rank_div_yield_forecast >= 0.7, d.rank_div_yield_forecast)),
         C('時価総額500億円以上', lambda d: _b(d.mktcap_oku >= 500, d.mktcap_oku)),
         C('自己資本比率40%以上', lambda d: _b(d.equity_ratio >= 0.4, d.equity_ratio), core=False),
         C('予想配当性向70%以下', lambda d: _b(d.payout_forecast <= 0.7, d.payout_forecast), core=False),
     ]},
    {'type': '高配当', 'investor': 'マイケル・オヒギンズ（ダウの犬／日本版）', 'style_label': '大型高配当ローテーション派',
     'summary': '時価総額上位100社の中で、予想配当利回りが高い上位10社を機械的に持つ。',
     'omitted': 'ダウ30銘柄 → 時価総額上位100社で代替',
     'conditions': [
         C('時価総額上位100社で配当利回り上位10', lambda d: _b(d.rank_div_in_top100 <= 10, d.mktcap_oku)),
     ]},
    {'type': '高配当', 'investor': 'ジェレミー・シーゲル（高配当×割安×持続性）', 'style_label': '持続高配当派',
     'summary': '配当利回りが高く、PERが高すぎず、配当性向に余裕があり、減配予想でない銘柄を長期で持つ。',
     'omitted': '長期の配当成長実績 → 当期の減配予想でないことで代替',
     'conditions': [
         C('予想配当利回りが市場の上位30%', lambda d: _b(d.rank_div_yield_forecast >= 0.7, d.rank_div_yield_forecast)),
         C('予想PERが市場の下位50%', lambda d: _b(d.rank_per_forecast <= 0.5, d.rank_per_forecast)),
         C('予想配当性向60%以下', lambda d: _b(d.payout_forecast <= 0.6, d.payout_forecast)),
         C('減配予想でない', lambda d: _b(d.div_growth_forecast >= 0, d.div_growth_forecast)),
         C('営業CFが黒字', lambda d: _b(d.cfo_positive == True, d.cfo_positive), core=False),  # noqa: E712
     ]},

    # =====================================================================
    # グロース
    # =====================================================================
    {'type': 'グロース', 'investor': 'ウィリアム・オニール（CAN SLIM）', 'style_label': '業績加速・新高値派',
     'summary': 'C=直近四半期の利益が前年比+25%、A=年間の利益成長+25%、N=新高値圏、S=需給（出来高増）、L=相対的な強さ上位、M=地合いが上向き。',
     'omitted': 'I（機関投資家の保有増）→ 省略。A（3年連続25%成長）→ 会社予想の成長率で代替',
     'conditions': [
         C('C: 直近四半期の営業利益が前年同期比+25%以上', lambda d: _b(d.op_yoy >= 0.25, d.op_yoy)),
         C('A: 予想EPS成長率+25%以上', lambda d: _b(d.eps_growth_forecast >= 0.25, d.eps_growth_forecast)),
         C('N: 52週高値の85%以上', lambda d: _b(d.high_52w_ratio >= 0.85, d.high_52w_ratio)),
         C('S: 売買代金が増加（20日/60日≧1）', lambda d: _b(d.value_ratio_20_60 >= 1.0, d.value_ratio_20_60), core=False),
         C('L: 12-1か月リターンが市場の上位20%', lambda d: _b(d.rank_mom_12_1 >= 0.8, d.rank_mom_12_1)),
         C('M: 50日線より上の銘柄が過半', lambda d: _b(d.market_breadth_50 >= 0.5, d.market_breadth_50)),
     ]},
    {'type': 'グロース', 'investor': 'ピーター・リンチ（GARP：PEGレシオ）', 'style_label': '成長割安（PEG）派',
     'summary': 'PER÷成長率（PEG）が1以下＝成長の割に安い。成長率は10〜50%、借金が少なく、中小型を好む。',
     'omitted': '在庫の増え方・事業のわかりやすさ → 省略',
     'conditions': [
         C('PEG≦1', lambda d: _b(d.peg <= 1.0, d.peg)),
         C('予想EPS成長率10〜50%', lambda d: _b(d.eps_growth_forecast.between(0.10, 0.50), d.eps_growth_forecast)),
         C('自己資本比率50%以上', lambda d: _b(d.equity_ratio >= 0.5, d.equity_ratio)),
         C('時価総額3,000億円以下', lambda d: _b(d.mktcap_oku <= 3000, d.mktcap_oku), core=False),
     ]},
    {'type': 'グロース', 'investor': 'マーク・ミネルヴィニ（トレンドテンプレート＋業績）', 'style_label': 'ステージ2成長株派',
     'summary': '株価が150日・200日線の上で200日線が上向き（上昇ステージ）、52週安値から+30%以上・高値から−25%以内、相対的強さ上位、利益成長あり。',
     'omitted': 'VCP（値幅収縮）の形の判定 → 省略',
     'conditions': [
         C('トレンドテンプレート（株価>150日線>200日線、200日線上向き、株価>50日線）',
           lambda d: _b(d.trend_template == True, d.trend_template)),  # noqa: E712
         C('52週安値から+30%以上', lambda d: _b(d.low_52w_ratio >= 1.3, d.low_52w_ratio)),
         C('52週高値から−25%以内', lambda d: _b(d.high_52w_ratio >= 0.75, d.high_52w_ratio)),
         C('12-1か月リターンが市場の上位30%', lambda d: _b(d.rank_mom_12_1 >= 0.7, d.rank_mom_12_1)),
         C('直近四半期のEPSが前年同期比+20%以上', lambda d: _b(d.eps_yoy >= 0.20, d.eps_yoy), core=False),
     ]},
    {'type': 'グロース', 'investor': 'フィリップ・フィッシャー（成長の質）', 'style_label': '高収益成長派',
     'summary': '売上が伸び、利益率が高く、利益の伸びが売上の伸びを上回る（利益率が改善している）、資本効率の高い会社。',
     'omitted': '経営陣・研究開発・営業力などの定性評価 → 省略',
     'conditions': [
         C('直近四半期の売上が前年同期比+10%以上', lambda d: _b(d.sales_yoy >= 0.10, d.sales_yoy)),
         C('営業利益率10%以上', lambda d: _b(d.op_margin_actual >= 0.10, d.op_margin_actual)),
         C('営業利益の伸び≧売上の伸び', lambda d: _b(d.op_yoy >= d.sales_yoy, d.op_yoy, d.sales_yoy)),
         C('ROE10%以上', lambda d: _b(d.roe >= 0.10, d.roe), core=False),
     ]},
    {'type': 'グロース', 'investor': 'DUKE。（新高値ブレイク投資術）', 'style_label': '新高値ブレイク成長派',
     'summary': '52週高値を更新した、増収増益（2桁成長）の中小型株を買う。',
     'omitted': '上場来高値・事業の新規性の判定 → 52週高値更新で代替',
     'conditions': [
         C('52週高値を更新', lambda d: _b(d.new_high_52w == True, d.new_high_52w)),  # noqa: E712
         C('直近四半期の売上が前年同期比+10%以上', lambda d: _b(d.sales_yoy >= 0.10, d.sales_yoy)),
         C('直近四半期の営業利益が前年同期比+10%以上', lambda d: _b(d.op_yoy >= 0.10, d.op_yoy)),
         C('時価総額2,000億円以下', lambda d: _b(d.mktcap_oku <= 2000, d.mktcap_oku), core=False),
     ]},

    # =====================================================================
    # モメンタム
    # =====================================================================
    {'type': 'モメンタム', 'investor': 'ジェガディーシュ＆ティットマン（12-1か月モメンタム）', 'style_label': '中期モメンタム派',
     'summary': '過去12か月（直近1か月を除く）の上昇率が上位10%の銘柄を買う。学術研究で最も有名なモメンタム。',
     'omitted': 'なし',
     'conditions': [C('12-1か月リターンが市場の上位10%', lambda d: _b(d.rank_mom_12_1 >= 0.9, d.rank_mom_12_1))]},
    {'type': 'モメンタム', 'investor': 'ニコラス・ダーバス（ボックス理論）', 'style_label': 'ボックスブレイク派',
     'summary': '株価が高値のボックスを上抜け、52週高値を出来高の増加とともに更新したときに買う。',
     'omitted': 'ボックスの上下限の目視判定 → 52週高値更新で代替',
     'conditions': [
         C('52週高値を更新', lambda d: _b(d.new_high_52w == True, d.new_high_52w)),  # noqa: E712
         C('売買代金が急増（20日/60日≧1.5）', lambda d: _b(d.value_ratio_20_60 >= 1.5, d.value_ratio_20_60)),
     ]},
    {'type': 'モメンタム', 'investor': 'リチャード・ドンチャン／タートルズ（チャネルブレイク）', 'style_label': 'チャネルブレイク派',
     'summary': '終値が過去55営業日の高値を上抜けたら買う（タートルズのシステム2）。',
     'omitted': 'なし',
     'conditions': [C('55日高値ブレイク', lambda d: _b(d.breakout_55 == True, d.breakout_55))]},  # noqa: E712
    {'type': 'モメンタム', 'investor': 'ジョージ＆ホァン（52週高値モメンタム）', 'style_label': '高値接近派',
     'summary': '株価が52週高値にどれだけ近いか（株価÷52週高値）が上位の銘柄を買う。',
     'omitted': 'なし',
     'conditions': [C('株価÷52週高値が市場の上位10%', lambda d: _b(d.rank_high_52w_ratio >= 0.9, d.rank_high_52w_ratio))]},
    {'type': 'モメンタム', 'investor': 'ゲイリー・アントナッチ（デュアルモメンタム）', 'style_label': 'デュアルモメンタム派',
     'summary': '相対モメンタム（他の銘柄より強い）と絶対モメンタム（自分自身が上がっている）の両方を満たすときだけ買う。',
     'omitted': '資産クラス間の切り替え → 個別株に適用',
     'conditions': [
         C('12-1か月リターンが市場の上位20%', lambda d: _b(d.rank_mom_12_1 >= 0.8, d.rank_mom_12_1)),
         C('12-1か月リターンがプラス', lambda d: _b(d.mom_12_1 > 0, d.mom_12_1)),
     ]},

    # =====================================================================
    # 優待（株主優待の内容データが無いため、優待投資家の「銘柄の選び方」の部分だけを代替）
    # =====================================================================
    {'type': '優待', 'investor': '桐谷広人（優待＋配当の総合利回り、下落時の買い増し）', 'style_label': '優待・配当長期保有派',
     'summary': '配当と優待を合わせた利回りを重視し、株価が大きく下がったときに買い増す。業績が安定した会社を長く持つ。',
     'omitted': '優待利回り（J-Quantsに優待データなし）→ 予想配当利回りのみで代替。信用取引をしない等 → 対象外',
     'conditions': [
         C('予想配当利回り2%以上', lambda d: _b(d.div_yield_forecast >= 2.0, d.div_yield_forecast)),
         C('株価が52週高値から−20%以上下落', lambda d: _b(d.high_52w_ratio <= 0.8, d.high_52w_ratio)),
         C('営業CFが黒字', lambda d: _b(d.cfo_positive == True, d.cfo_positive)),  # noqa: E712
         C('自己資本比率40%以上', lambda d: _b(d.equity_ratio >= 0.4, d.equity_ratio), core=False),
     ]},
    {'type': '優待', 'investor': 'みきまるファンド（優待バリュー）', 'style_label': '優待バリュー小型株派',
     'summary': '割安（低PER・低PBR）でキャッシュリッチな小型株を、優待をおまけとして分散して持つ。',
     'omitted': '優待の有無・内容（データなし）→ 省略。ネットキャッシュ比率 → 現金÷時価総額で代替',
     'conditions': [
         C('予想PER12倍以下', lambda d: _b((d.per_forecast > 0) & (d.per_forecast <= 12), d.per_forecast)),
         C('PBR1倍以下', lambda d: _b(d.pbr <= 1.0, d.pbr)),
         C('時価総額500億円以下', lambda d: _b(d.mktcap_oku <= 500, d.mktcap_oku)),
         C('現金が時価総額の30%以上', lambda d: _b(d.cash_to_mktcap >= 0.3, d.cash_to_mktcap)),
         C('減益予想でない', lambda d: _b(d.op_growth_forecast >= 0, d.op_growth_forecast), core=False),
     ]},

    # =====================================================================
    # バリュー・逆張り
    # =====================================================================
    {'type': 'バリュー', 'investor': 'ウォーレン・バフェット（高収益・低負債の優良企業を適正価格で）', 'style_label': '優良企業長期保有派',
     'summary': 'ROEが高く、借金が少なく、本業で現金を稼ぎ、利益率が高い会社を、高すぎない価格で買う。',
     'omitted': '経済的な堀（競争優位）の定性判断・10年間のROE安定性 → 直近の数値で代替',
     'conditions': [
         C('ROE15%以上', lambda d: _b(d.roe >= 0.15, d.roe)),
         C('自己資本比率50%以上', lambda d: _b(d.equity_ratio >= 0.5, d.equity_ratio)),
         C('営業CFが黒字', lambda d: _b(d.cfo_positive == True, d.cfo_positive)),  # noqa: E712
         C('営業利益率15%以上', lambda d: _b(d.op_margin_actual >= 0.15, d.op_margin_actual)),
         C('予想PER20倍以下', lambda d: _b((d.per_forecast > 0) & (d.per_forecast <= 20), d.per_forecast), core=False),
     ]},
    {'type': 'バリュー', 'investor': 'ジョエル・グリーンブラット（魔法の公式）', 'style_label': '魔法の公式派',
     'summary': '益回り（営業利益÷企業価値）と資本利益率（営業利益÷事業資産）の順位を合計し、上位を買う。',
     'omitted': '金融・公益の除外 → 業種コードが無い銘柄もあるため省略',
     'conditions': [C('益回り順位と資本利益率順位の平均が上位20%',
                      lambda d: _b(d.greenblatt_rank >= 0.8, d.greenblatt_rank))]},
    {'type': 'バリュー', 'investor': 'ベンジャミン・グレアム（ネットネット株の考え方）', 'style_label': '資産バリュー派',
     'summary': '会社が持つ現金などの資産に比べて、時価総額が極端に小さい銘柄を買う。',
     'omitted': '正味流動資産（流動資産−総負債）→ データに流動資産が無いため「現金÷時価総額」で近似',
     'conditions': [
         C('現金が時価総額の80%以上', lambda d: _b(d.cash_to_mktcap >= 0.8, d.cash_to_mktcap)),
         C('PBR0.7倍以下', lambda d: _b(d.pbr <= 0.7, d.pbr)),
     ]},
    {'type': 'バリュー', 'investor': 'ジョセフ・ピオトロスキ（Fスコア＋低PBR）', 'style_label': '低PBR財務改善派',
     'summary': 'PBRが低い銘柄の中から、黒字・営業CF黒字・増益・利益率改善など財務が良くなっている会社を選ぶ。',
     'omitted': '9項目のうち流動比率・発行株式数・総資産回転率 → 省略（測れる項目のみ）',
     'conditions': [
         C('PBRが市場の下位30%', lambda d: _b(d.rank_pbr <= 0.3, d.rank_pbr)),
         C('ROEがプラス', lambda d: _b(d.roe > 0, d.roe)),
         C('営業CFが黒字', lambda d: _b(d.cfo_positive == True, d.cfo_positive)),  # noqa: E712
         C('直近四半期の営業利益が前年同期比プラス', lambda d: _b(d.op_yoy > 0, d.op_yoy)),
         C('営業利益の伸び≧売上の伸び（利益率改善）', lambda d: _b(d.op_yoy >= d.sales_yoy, d.op_yoy, d.sales_yoy),
           core=False),
     ]},
    {'type': 'バリュー', 'investor': 'BNF（25日移動平均線からの乖離率による逆張り）', 'style_label': '乖離率逆張り派',
     'summary': '株価が25日線から大きく下に離れた（売られすぎた）銘柄を、反発を狙って短期で買う。',
     'omitted': '板・需給の裁量判断 → 省略',
     'conditions': [C('25日線から−20%以上の乖離', lambda d: _b(d.dist_ma25 <= -0.20, d.dist_ma25))]},
]


def evaluate_model(df, model):
    """各行の (合格, スコア, 条件ごとの値) を返す。"""
    conds = {}
    num = pd.Series(0.0, index=df.index)
    den = pd.Series(0.0, index=df.index)
    total_w = sum(c['weight'] for c in model['conditions'])
    passed = pd.Series(True, index=df.index)
    for c in model['conditions']:
        v = c['fn'](df)
        conds[c['label']] = v
        num += (v.fillna(0) * c['weight'])
        den += v.notna() * c['weight']
        if c['core']:
            passed &= (v == 1.0)
    score = (num / den * 100).where(den >= 0.6 * total_w)
    return passed, score, conds
