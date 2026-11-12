# -*- coding: utf-8 -*-
"""スイング型③の修正の数字による確認・決算発表予定・PDF読み取りの単体テスト（python test_swing_disclosures.py）。"""
import disclosure_numbers as dn
import earnings_calendar as ec
import swing_live as sl


def test_verify_revisions():
    up = {'item': '営業利益', 'change_pct': 12.0, 'source': 'XBRL'}
    small = {'item': '営業利益', 'change_pct': 2.0, 'source': 'XBRL'}
    table = {'a': up, 'b': small, 'c': None, 'd': None}
    items = [{'ticker': 'A', 'disclosures': [{'url': 'a', 'title_up': False}]},   # +12% → 残す
             {'ticker': 'B', 'disclosures': [{'url': 'b', 'title_up': True}]},    # +2%（タイトルは上方）→ 外す
             {'ticker': 'C', 'disclosures': [{'url': 'c', 'title_up': True}]},    # 読めない・タイトル上方 → 確認できずで残す
             {'ticker': 'D', 'disclosures': [{'url': 'd', 'title_up': False}]}]   # 読めない・方向不明 → 外す
    kept = sl.verify_revisions(items, reader=lambda d: table[d['url']])
    assert [x['ticker'] for x in kept] == ['A', 'C']
    assert kept[0]['revision']['change_pct'] == 12.0 and kept[1]['revision']['change_pct'] is None


def test_revision_titles():
    assert sl.is_revision_candidate('業績予想の修正に関するお知らせ')
    assert sl.is_revision_candidate('通期業績予想の上方修正に関するお知らせ')
    assert not sl.is_revision_candidate('業績予想の下方修正に関するお知らせ')
    assert not sl.is_revision_candidate('自己株式の取得に関するお知らせ')


def test_pdf_table():
    text = ('　 売上高 営業利益 経常利益 親会社株主に帰属する当期純利益 1株当たり当期純利益\n'
            '前回発表予想（A） 10,000 1,000 1,000 600 50.00\n'
            '今回修正予想（B） 11,000 1,200 1,150 700 58.33\n'
            '増減額（B-A） 1,000 200 150 100\n'
            '増減率（％） 10.0 20.0 15.0 16.7\n')
    assert dn.revision_from_pdf_text(text) == {'item': '営業利益', 'change_pct': 20.0, 'source': 'PDF'}
    assert dn.revision_from_pdf_text('増減率（％） 10.0\n売上高 営業利益\n') is None  # 列の数が合わない → 読まない


def test_xbrl_parse():
    html = ('<ix:nonFraction name="tse-ed-t:ChangeInOperatingIncome" '
            'contextRef="CurrentYearDuration_ConsolidatedMember_CurrentMember_ForecastMember" scale="-2">36.4</ix:nonFraction>'
            '<ix:nonFraction name="tse-ed-t:OperatingIncome" '
            'contextRef="CurrentYearDuration_ConsolidatedMember_CurrentMember_ForecastMember" scale="6">4,500</ix:nonFraction>')
    assert dn.revision_from_xbrl(dn.parse_ixbrl(html)) == {'item': '営業利益', 'change_pct': 36.4, 'source': 'XBRL'}
    html2 = ('<ix:nonFraction name="tse-ed-t:OrdinaryIncome" '
             'contextRef="CurrentYearDuration_NonConsolidatedMember_CurrentMember_ForecastMember">1,100</ix:nonFraction>'
             '<ix:nonFraction name="tse-ed-t:OrdinaryIncome" '
             'contextRef="CurrentYearDuration_NonConsolidatedMember_PreviousMember_ForecastMember">1,000</ix:nonFraction>')
    assert dn.revision_from_xbrl(dn.parse_ixbrl(html2))['change_pct'] == 10.0  # 増減率が無いときは計算する


def test_earnings_calendar():
    cal = {'2026-11-12': {'8022.T': {}}, '2026-11-13': {'7203.T': {}}}
    assert ec.next_announcements(cal, '2026-11-11') == ('2026-11-12', {'8022.T': {}})
    assert ec.next_announcements(cal, '2026-11-13') == (None, {})
    assert ec.code_to_ticker('80220') == '8022.T' and ec.code_to_ticker('130A0') == '130A.T'


if __name__ == '__main__':
    for name, fn in list(globals().items()):
        if name.startswith('test_'):
            fn()
            print('ok', name)
