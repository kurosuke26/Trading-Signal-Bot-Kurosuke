#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
disclosure_numbers.py — 業績予想の修正（TDnet）の数字を読み取る（2026-11-12追加）。

スイング型③（上方修正後の継続）で、検証と同じ「会社予想が前回から +5%以上 上がったか」を本番でも確かめるために使う。

【読み取りの順番】
1. XBRL（TDnet の url_xbrl。数字だけのデータ。tse-rvfc-*.zip の中の ixbrl.htm）
   - 通期（CurrentYearDuration）の「増減率（ChangeIn〜）」を読む。範囲予想（〜）は下限（LowerMember）を使う（控えめに見る）
   - 増減率が無い（前回予想が0・赤字など）ときは、今回（CurrentMember）と前回（PreviousMember）の値から計算する
   - 項目は 営業利益 → 経常利益 → 純利益 の順（検証の evaluate_swing.revision_disclosures と同じ優先順位）
2. PDF（XBRL が無いとき）：1ページ目の表の「増減率（％）」の行と、項目名の並び（売上高・営業利益…）が
   はっきり対応するときだけ読む。表の形が会社ごとに違うため、少しでも合わなければ「確認できず」にする（推測はしない）
3. どちらも読めなければ None（＝確認できず）

戻り値：{'item': '営業利益'など, 'change_pct': float, 'source': 'XBRL'|'PDF'} または None
"""

import io
import re
import zipfile

import requests

UA = {'User-Agent': 'Mozilla/5.0 (KurosukeBot disclosure_numbers.py)'}
TIMEOUT = 30
MIRROR_PREFIX = 'https://webapi.yanoshin.jp/rd.php?'
ITEMS = [('OperatingIncome', '営業利益'), ('OrdinaryIncome', '経常利益'),
         ('ProfitAttributableToOwnersOfParent', '純利益'), ('NetIncome', '純利益'), ('Profit', '純利益')]


def _direct(url):
    """やのしんのリダイレクトURLを、TDnet本体のURLに直す（取得先を1つに減らす）。"""
    return url[len(MIRROR_PREFIX):] if url and url.startswith(MIRROR_PREFIX) else url


def _get(url, session=None):
    r = (session or requests).get(_direct(url), headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return r.content


# ---------------------------------------------------------------------------
# XBRL
# ---------------------------------------------------------------------------

_TAG = re.compile(r'<ix:nonFraction\b([^>]*)>(.*?)</ix:nonFraction>', re.S)


def parse_ixbrl(html):
    """{(項目名, contextRef): 数値（表示どおり。%はそのまま）}"""
    out = {}
    for attrs, inner in _TAG.findall(html):
        name = re.search(r'name="(?:[^":]+:)?([^"]+)"', attrs)
        ctx = re.search(r'contextRef="([^"]+)"', attrs)
        if not name or not ctx:
            continue
        txt = re.sub(r'<[^>]+>', '', inner).replace(',', '').strip()
        try:
            v = float(txt)
        except ValueError:
            continue
        if 'sign="-"' in attrs:
            v = -v
        out[(name.group(1), ctx.group(1))] = v
    return out


def _pick(vals, name, period_member):
    """通期・連結（無ければ個別）の値。範囲予想は下限を優先。"""
    for scope in ('ConsolidatedMember', 'NonConsolidatedMember'):
        for kind in ('ForecastMember', 'LowerMember', ''):
            for (n, ctx), v in vals.items():
                if n != name or not ctx.startswith('CurrentYearDuration') or scope + '_' not in ctx + '_':
                    continue
                if f'_{period_member}' not in ctx:
                    continue
                if kind and not ctx.endswith(kind):
                    continue
                if not kind and ('UpperMember' in ctx):
                    continue
                return v
    return None


def revision_from_xbrl(vals):
    for key, label in ITEMS:
        rate = _pick(vals, 'ChangeIn' + key, 'CurrentMember')
        if rate is not None:
            return {'item': label, 'change_pct': round(rate, 1), 'source': 'XBRL'}
        cur, prev = _pick(vals, key, 'CurrentMember'), _pick(vals, key, 'PreviousMember')
        if cur is not None and prev is not None and prev > 0:
            return {'item': label, 'change_pct': round((cur / prev - 1) * 100, 1), 'source': 'XBRL'}
    return None


def read_xbrl_zip(content):
    z = zipfile.ZipFile(io.BytesIO(content))
    names = [n for n in z.namelist() if n.endswith('.htm') and 'rvfc' in n] or \
            [n for n in z.namelist() if n.endswith('ixbrl.htm')]
    for n in names:
        r = revision_from_xbrl(parse_ixbrl(z.read(n).decode('utf-8', errors='replace')))
        if r:
            return r
    return None


# ---------------------------------------------------------------------------
# PDF（表の形がはっきりしているときだけ）
# ---------------------------------------------------------------------------

_HEADER_WORDS = [('売上高', None), ('営業収益', None), ('売上収益', None), ('営業利益', '営業利益'),
                 ('経常利益', '経常利益'), ('当期純利益', '純利益'), ('帰属する', '純利益')]
_NUM = r'[－\-–]?\d+(?:\.\d+)?|[－―\-–]'


def revision_from_pdf_text(text):
    lines = [re.sub(r'\s+', ' ', ln).strip() for ln in text.splitlines()]
    rate_lines = [ln for ln in lines if re.match(r'^増減率\s*[（(]\s*[％%]\s*[)）]', ln)]
    headers = [ln for ln in lines if '営業利益' in ln or '経常利益' in ln]
    if len(rate_lines) != 1 or len(headers) < 1:
        return None
    toks = re.findall(_NUM, rate_lines[0].split(')', 1)[-1].split('）', 1)[-1])
    if any('〜' in ln or '～' in ln for ln in (rate_lines[0],)):
        return None  # 範囲予想は対応が取りにくいので読まない
    header = headers[0]
    eps_pos = header.find('1株当たり')
    body = header if eps_pos < 0 else header[:eps_pos]  # 「1株当たり当期純利益」を純利益の列と数えない
    cols = []
    for label, words in (('売上', ('売上高', '営業収益', '売上収益')), ('営業利益', ('営業利益',)),
                         ('経常利益', ('経常利益',)), ('純利益', ('帰属する', '当期純利益', '純利益'))):
        pos = [body.find(w) for w in words if body.find(w) >= 0]
        if pos:
            cols.append((min(pos), label))
    cols.sort()
    # 増減率の行は、1株当たり利益の列が空のことが多い（列の数が「項目数」か「項目数＋1株当たり」なら読む）
    if not cols or len(toks) not in (len(cols), len(cols) + (1 if eps_pos >= 0 else 0)):
        return None
    cols = [(k, None, label) for k, label in cols]
    for want in ('営業利益', '経常利益', '純利益'):
        for k, (_, word, label) in enumerate(cols):
            if label == want:
                t = toks[k].replace('－', '-').replace('–', '-')
                if t in ('-', '―'):
                    break
                try:
                    return {'item': want, 'change_pct': float(t), 'source': 'PDF'}
                except ValueError:
                    break
    return None


def read_pdf(content):
    import pypdf
    reader = pypdf.PdfReader(io.BytesIO(content))
    text = reader.pages[0].extract_text() or ''
    return revision_from_pdf_text(text)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def revision_numbers(disclosure, session=None):
    """disclosure: {'url_xbrl', 'url'(PDF)}。読めなければ None。通信や解析の失敗も None（呼び出し側は「確認できず」として扱う）。"""
    try:
        if disclosure.get('url_xbrl'):
            r = read_xbrl_zip(_get(disclosure['url_xbrl'], session))
            if r:
                return r
    except Exception as e:  # noqa: BLE001
        print(f"[disclosure] XBRLの読み取りに失敗: {e}")
    try:
        if disclosure.get('url'):
            return read_pdf(_get(disclosure['url'], session))
    except Exception as e:  # noqa: BLE001
        print(f"[disclosure] PDFの読み取りに失敗: {e}")
    return None
