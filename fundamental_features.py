#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fundamental_features.py — J-Quantsの開示データ（fins.json）を「開示日順に1件ずつ読み進める」形で、
ある日 t 時点で知り得たファンダメンタルズ（実績・会社予想・修正・前年同期比・進捗率）を組み立てる
モジュール（2026-09-16追加。検証スクリプト用）。

【先読みを防ぐ仕組み】
開示は (DiscDate, DiscTime, DiscNo) の順に並べ、t日までに開示日が来たものだけを状態に反映する。
t日の引け後（15:30以降）の開示も t日の状態に含めるが、検証側のエントリーは t+1日の始値なので、
「開示を見てから翌朝に動く」実際の流れと一致する。

【株式分割】
1株あたりの値（EPS・配当・BPS）は、開示日時点の株数ベースで書かれている。
AdjFactor（分割係数）の累積積 cum を使い、値を「cum=1の基準株数」に正規化して保持し（v / cum[開示日]）、
t日に取り出すときに cum[t] を掛けて t日の株数ベースに戻す。t日までに起きた分割だけが反映される。

【扱う開示の種類】
- 決算短信（FY/1Q/2Q/3Q FinancialStatements）：実績、当期予想（F*）、FYなら翌期予想（NxF*）
- 業績予想の修正（EarnForecastRevision）：当期予想（F*）の更新
- 配当予想の修正（DividendForecastRevision）：当期の年間配当予想（FDivAnn）の更新
連結の値を優先し、無ければ個別（NC*）を使う。
"""

import json
import os

import numpy as np

CACHE_DIR = os.getenv('JQUANTS_CACHE_DIR') or 'data/jquants_cache'
QUARTER_SHARE = {'1Q': 0.25, '2Q': 0.5, '3Q': 0.75}
PER_SHARE_KEYS = ('eps', 'div', 'bps')


def _num(rec, *keys):
    for k in keys:
        v = rec.get(k)
        if v in (None, ''):
            continue
        try:
            x = float(v)
        except (TypeError, ValueError):
            continue
        if np.isfinite(x):
            return x
    return None


def load_fins_records(ticker4):
    path = os.path.join(CACHE_DIR, ticker4, 'fins.json')
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding='utf-8') as f:
            rows = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    rows = [r for r in rows if isinstance(r, dict) and r.get('DiscDate')]
    return sorted(rows, key=lambda r: (r.get('DiscDate') or '', r.get('DiscTime') or '', r.get('DiscNo') or ''))


def _prev_fy(fy_end):
    """'2026-03-31' -> '2025-03-31'（前年の同じ期末）。"""
    if not fy_end or len(fy_end) < 10:
        return None
    return f'{int(fy_end[:4]) - 1}{fy_end[4:]}'


class FundamentalState:
    """開示を1件ずつ適用していき、任意の時点の特徴量を返す。"""

    def __init__(self, cum_factor_at_date):
        """cum_factor_at_date: 'YYYY-MM-DD' -> その日（以前の最後の営業日）の分割係数の累積積 を返す関数。"""
        self.cum_at = cum_factor_at_date
        self.forecast = {}        # fy_end -> {'eps','div','op','sales','np'}（per-shareは正規化済み）
        self.forecast_first = {}  # fy_end -> 期初（最初に出た）予想
        self.actual = {}          # fy_end -> FY決算の実績
        self.statements = {}      # (CurPerType, fy_end) -> 決算短信の値
        self.latest_statement = None
        self.latest_disc_date = None
        self.dividend_events = []  # 増配・減配の発表（検証用）

    # ---- 適用 ----
    def _norm(self, v, disc_date):
        return None if v is None else v / self.cum_at(disc_date)

    def _set_forecast(self, fy_end, values, disc_date, source):
        if not fy_end:
            return
        values = {k: v for k, v in values.items() if v is not None}
        if not values:
            return
        cur = self.forecast.setdefault(fy_end, {})
        prev_div = cur.get('div')
        cur.update(values)
        first = self.forecast_first.setdefault(fy_end, {})
        for k, v in values.items():
            first.setdefault(k, v)
        # 配当予想の変化（増配・減配の発表）を記録する
        new_div = values.get('div')
        if new_div is not None:
            base_prev_forecast = prev_div
            base_prev_actual = (self.actual.get(_prev_fy(fy_end)) or {}).get('div')
            self.dividend_events.append({
                'disc_date': disc_date, 'fy_end': fy_end, 'source': source,
                'new_div_norm': new_div, 'prev_forecast_norm': base_prev_forecast,
                'prev_actual_norm': base_prev_actual,
            })

    def apply(self, rec):
        disc = rec['DiscDate']
        doc = rec.get('DocType') or ''
        per = rec.get('CurPerType') or ''
        fy_end = rec.get('CurFYEn') or ''
        self.latest_disc_date = disc

        def fvals(prefix):
            p = prefix
            return {
                'eps': self._norm(_num(rec, f'{p}EPS', f'{p}NCEPS'), disc),
                'div': self._norm(_num(rec, f'{p}DivAnn'), disc),
                'op': _num(rec, f'{p}OP', f'{p}NCOP'),
                'sales': _num(rec, f'{p}Sales', f'{p}NCSales'),
                'np': _num(rec, f'{p}NP', f'{p}NCNP', f'{p}Np'),
            }

        if 'FinancialStatements' in doc:
            st = {
                'per': per, 'fy_end': fy_end, 'disc': disc,
                'sales': _num(rec, 'Sales', 'NCSales'), 'op': _num(rec, 'OP', 'NCOP'),
                'np': _num(rec, 'NP', 'NCNP'), 'eps': self._norm(_num(rec, 'EPS', 'NCEPS'), disc),
                'bps': self._norm(_num(rec, 'BPS', 'NCBPS'), disc),
                'ta': _num(rec, 'TA', 'NCTA'), 'eq': _num(rec, 'Eq', 'NCEq'),
                'eq_ratio': _num(rec, 'EqAR', 'NCEqAR'), 'cash': _num(rec, 'CashEq'),
                'cfo': _num(rec, 'CFO'), 'div': self._norm(_num(rec, 'DivAnn'), disc),
                'shares': _num(rec, 'ShOutFY'), 'treasury': _num(rec, 'TrShFY'),
            }
            self.statements[(per, fy_end)] = st
            self.latest_statement = st
            if per == 'FY':
                self.actual[fy_end] = st
                nxt = rec.get('NxtFYEn') or ''
                v = fvals('NxF')
                if v.get('np') is None:
                    v['np'] = _num(rec, 'NxFNp', 'NxFNCNP')
                self._set_forecast(nxt, v, disc, 'FY決算（翌期予想）')
            else:
                self._set_forecast(fy_end, fvals('F'), disc, f'{per}決算')
        elif doc == 'EarnForecastRevision':
            v = fvals('F')
            v['div'] = None
            self._set_forecast(fy_end, v, disc, '業績予想の修正')
        elif doc == 'DividendForecastRevision':
            self._set_forecast(fy_end, {'div': self._norm(_num(rec, 'FDivAnn'), disc)}, disc, '配当予想の修正')

    # ---- 取り出し ----
    def features(self, t_date, cum_t, raw_price, mktcap_mil):
        """t日時点の特徴量 dict。値が無い項目は None。"""
        f = {}
        # 進行中の期：予想はあるが実績（FY決算）がまだ出ていない期のうち最も古いもの。
        # 決算期変更などで実績が出ないまま放置された古い期は除く（期末から120日超）
        stale_before = str(np.datetime64(t_date) - np.timedelta64(120, 'D'))
        pending = sorted(fy for fy in self.forecast if fy not in self.actual and fy >= stale_before)
        cur_fy = pending[0] if pending else None
        # 比較対象の前期実績：進行中の期の1年前。無ければ最新の実績
        prev_fy = _prev_fy(cur_fy) if cur_fy and _prev_fy(cur_fy) in self.actual else             (max(self.actual) if self.actual else None)
        fc = self.forecast.get(cur_fy, {}) if cur_fy else {}
        fc0 = self.forecast_first.get(cur_fy, {}) if cur_fy else {}
        act = self.actual.get(prev_fy, {}) if prev_fy else {}
        st = self.latest_statement or {}

        def ps(v):  # 正規化済みの1株あたり値 → t日の株数ベース
            return None if v is None else v * cum_t

        f_eps, f_div = ps(fc.get('eps')), ps(fc.get('div'))
        a_eps, a_div, bps = ps(act.get('eps')), ps(act.get('div')), ps(act.get('bps'))
        price = raw_price if raw_price and raw_price > 0 else None

        def ratio(a, b):
            return a / b if a is not None and b not in (None, 0) else None

        def growth(a, b):
            return a / b - 1 if a is not None and b is not None and b > 0 else None

        f['cur_fy'] = cur_fy
        f['per_forecast'] = ratio(price, f_eps) if f_eps and f_eps > 0 else None
        f['per_actual'] = ratio(price, a_eps) if a_eps and a_eps > 0 else None
        f['pbr'] = ratio(price, bps) if bps and bps > 0 else None
        f['div_yield_forecast'] = ratio(f_div, price) * 100 if f_div is not None and price else None
        f['div_yield_actual'] = ratio(a_div, price) * 100 if a_div is not None and price else None
        f['payout_forecast'] = ratio(f_div, f_eps) if f_eps and f_eps > 0 and f_div is not None else None
        f['eps_growth_forecast'] = growth(f_eps, a_eps)
        f['op_growth_forecast'] = growth(fc.get('op'), act.get('op'))
        f['sales_growth_forecast'] = growth(fc.get('sales'), act.get('sales'))
        f['div_growth_forecast'] = growth(f_div, a_div)
        f['op_revision_from_initial'] = growth(fc.get('op'), fc0.get('op'))
        f['eps_revision_from_initial'] = growth(fc.get('eps'), fc0.get('eps'))
        f['div_revision_from_initial'] = growth(fc.get('div'), fc0.get('div'))
        f['roe'] = ratio(act.get('np'), act.get('eq'))
        f['op_margin_actual'] = ratio(act.get('op'), act.get('sales'))
        f['cfo_positive'] = (act.get('cfo') > 0) if act.get('cfo') is not None else None
        f['equity_ratio'] = st.get('eq_ratio')
        cash = st.get('cash') if st.get('cash') is not None else act.get('cash')
        mcap_yen = mktcap_mil * 1e6 if mktcap_mil else None
        f['mktcap_oku'] = mktcap_mil / 100 if mktcap_mil else None  # 億円
        f['cash_to_mktcap'] = ratio(cash, mcap_yen)
        liab = (st.get('ta') - st.get('eq')) if st.get('ta') is not None and st.get('eq') is not None else None
        ev = (mcap_yen + (liab or 0) - (cash or 0)) if mcap_yen else None
        f['earnings_yield_ev'] = ratio(act.get('op'), ev) if ev and ev > 0 else None
        f['roc'] = ratio(act.get('op'), (st.get('ta') - (cash or 0)) if st.get('ta') else None)

        # 決算進捗率（四半期決算の累計営業利益 ÷ 通期予想 − 経過割合）
        f['op_progress_excess'] = None
        if st.get('per') in QUARTER_SHARE and st.get('fy_end') == cur_fy and fc.get('op') and fc['op'] > 0 \
                and st.get('op') is not None:
            f['op_progress_excess'] = st['op'] / fc['op'] - QUARTER_SHARE[st['per']]

        # 直近の決算の前年同期比
        f['sales_yoy'] = f['op_yoy'] = f['np_yoy'] = f['eps_yoy'] = None
        if st:
            prev = self.statements.get((st.get('per'), _prev_fy(st.get('fy_end'))))
            if prev:
                f['sales_yoy'] = growth(st.get('sales'), prev.get('sales'))
                f['op_yoy'] = growth(st.get('op'), prev.get('op'))
                f['np_yoy'] = growth(st.get('np'), prev.get('np'))
                f['eps_yoy'] = growth(st.get('eps'), prev.get('eps'))
        f['days_since_disclosure'] = None
        if self.latest_disc_date:
            f['days_since_disclosure'] = (np.datetime64(t_date) - np.datetime64(self.latest_disc_date)).astype(int)
        return f
