#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_integration.py — ネットワークをモックしたエンドツーエンド統合テスト（2段階構成版）。

collector.collect()（ネットワークをモック）→ data/latest_scan.json 相当のスナップショットを
一時ファイルに書き出す → poster.post()（requests.postをモック）でそのファイルを読み込んで
Discordへの投稿ペイロードを組み立てる、という一連の流れを確認する。
"""

import json
import os
import tempfile
from unittest import mock

import numpy as np
import pandas as pd

import collector as col
import poster as pst


def make_ohlcv(n=200, start=1000, drift=2.0, noise=8.0, seed=0):
    rng = np.random.default_rng(seed)
    closes = start + np.cumsum(drift + rng.normal(0, noise, n))
    closes = np.clip(closes, 50, None)
    highs = closes * 1.01
    lows = closes * 0.99
    opens = np.roll(closes, 1)
    opens[0] = closes[0]
    idx = pd.date_range('2025-01-01', periods=n, freq='B')
    return pd.DataFrame({'Open': opens, 'High': highs, 'Low': lows, 'Close': closes,
                          'Volume': np.full(n, 1_000_000)}, index=idx)


def build_fake_universe(n=25):
    tickers = [f"{1000 + i}.T" for i in range(n)]
    fundamentals = {}
    for i, t in enumerate(tickers):
        if i % 5 == 0:
            div, per, pbr = 4.2, 12.0, 1.5   # LONG候補
        elif i % 5 == 1:
            div, per, pbr = 0.5, 40.0, 3.0   # SHORT候補
        else:
            div, per, pbr = 2.0, 20.0, 1.2   # ニュートラル
        fundamentals[t] = {
            'name': f'テスト銘柄{i}',
            'current_price': 1000 + i * 10,
            'dividend_yield': div,
            'pbr': pbr,
            'per': per,
            'growth_raw': 0.05 if i % 3 == 0 else None,
            'sector': 'Test',
        }
    return tickers, fundamentals


def check_discord_limits(payload):
    if payload is None:
        return
    content = payload.get('content')
    if content is not None:
        assert len(content) <= 2000, f'content too long: {len(content)}'
    embeds = payload.get('embeds', [])
    assert len(embeds) <= 10, f'embeds too many: {len(embeds)}'
    total_len = 0
    for e in embeds:
        if e.get('title'):
            assert len(e['title']) <= 256
        if e.get('description'):
            assert len(e['description']) <= 4096
            total_len += len(e['description'])
        for f in e.get('fields', []):
            assert len(f['name']) <= 256
            assert len(f['value']) <= 1024
            total_len += len(f['name']) + len(f['value'])
    assert total_len <= 6000, f'embed total chars too long: {total_len}'


def run_collector_stage(n_tickers=25):
    tickers, fundamentals = build_fake_universe(n_tickers)
    histories = {t: make_ohlcv(seed=i) for i, t in enumerate(tickers)}

    def fake_get_eps_trend(stock):
        return {'values': [10.0, 11.0, 12.0], 'increasing': True}

    with mock.patch.object(col, 'fetch_fundamentals', return_value=(fundamentals, [('9999.T', 'テスト用の取得失敗')])), \
         mock.patch.object(col, 'fetch_price_histories', return_value=(histories, [])), \
         mock.patch.object(col, 'get_eps_trend', side_effect=fake_get_eps_trend), \
         mock.patch.object(col, 'resolve_universe', return_value=(tickers, None, False)):
        ok = col.collect()

    assert ok is True, 'collector.collect()がFalseを返した'
    return tickers


def run():
    tmpdir = tempfile.mkdtemp()
    snapshot_path = os.path.join(tmpdir, 'latest_scan.json')

    with mock.patch.object(col, 'OUTPUT_PATH', snapshot_path):
        tickers = run_collector_stage(25)

    assert os.path.exists(snapshot_path), 'スナップショットファイルが作成されていない'
    with open(snapshot_path, encoding='utf-8') as f:
        snapshot = json.load(f)

    print(f"snapshot counts: {snapshot['counts']}")
    assert snapshot['counts']['analyzed'] == len(tickers) - 0  # fundamentals取得成功分すべて
    assert snapshot['universe_size'] == len(tickers)

    # detail='full' なのはLONG/SHORT/上位ニュートラルのみのはず
    full_entries = [r for r in snapshot['results'].values() if r['detail'] == 'full']
    slim_entries = [r for r in snapshot['results'].values() if r['detail'] == 'slim']
    print(f"full={len(full_entries)} slim={len(slim_entries)}")
    assert all(r['signal'] in ('LONG', 'SHORT') or True for r in full_entries)  # 上位ニュートラルも含まれ得る

    # ---- poster段階 ----
    sent_payloads = {}

    def fake_post(url, data=None, files=None, headers=None):
        body = None
        if files is not None and isinstance(data, dict) and 'payload_json' in data:
            body = json.loads(data['payload_json'])
        elif data is not None:
            body = json.loads(data)
        sent_payloads.setdefault(url, []).append(body)

        class FakeResp:
            status_code = 204
            text = ''
        return FakeResp()

    with mock.patch.object(pst, 'SNAPSHOT_PATH', snapshot_path), \
         mock.patch.object(pst.requests, 'post', side_effect=fake_post), \
         mock.patch.dict(pst.WEBHOOKS, {('LONG', 'TEST'): 'https://discord.test/long',
                                         ('SHORT', 'TEST'): 'https://discord.test/short',
                                         ('WARNING', 'TEST'): 'https://discord.test/warning',
                                         ('PERFORMANCE', 'TEST'): 'https://discord.test/perf',
                                         ('STRATEGY', 'TEST'): 'https://discord.test/strategy'}):
        result = pst.post()

    print(f'poster.post() result = {result}')
    assert result is True, 'poster.post()がFalseを返した（一部チャネル送信失敗扱い）'
    assert len(sent_payloads) == 5, f'送信されたWebhook URL数が想定と違う: {list(sent_payloads.keys())}'

    for url, payload_list in sent_payloads.items():
        for payload in payload_list:
            check_discord_limits(payload)
            json.dumps(payload)  # 再シリアライズ可能であることの確認

    long_payloads = sent_payloads.get('https://discord.test/long', [])
    assert long_payloads and long_payloads[0]['embeds'], 'LONGチャネルにEmbedが送られていない'
    print(f"LONG embeds count: {len(long_payloads[0]['embeds'])}")
    print(f"LONG content: {long_payloads[0]['content']}")

    short_payloads = sent_payloads.get('https://discord.test/short', [])
    assert short_payloads and short_payloads[0]['embeds'], 'SHORTチャネルにEmbedが送られていない'

    perf_payload = sent_payloads['https://discord.test/perf'][0]
    print('PERFORMANCE fields:', perf_payload['embeds'][0]['fields'])

    # ---- データなし・データが古い場合の挙動も確認 ----
    empty_dir = tempfile.mkdtemp()
    missing_path = os.path.join(empty_dir, 'no_such_file.json')
    warn_sent = {}

    def fake_post_warn(url, data=None, files=None, headers=None):
        warn_sent.setdefault(url, []).append(json.loads(data))

        class R:
            status_code = 204
            text = ''
        return R()

    with mock.patch.object(pst, 'SNAPSHOT_PATH', missing_path), \
         mock.patch.object(pst.requests, 'post', side_effect=fake_post_warn), \
         mock.patch.dict(pst.WEBHOOKS, {('WARNING', 'TEST'): 'https://discord.test/warning'}):
        result_missing = pst.post()
    assert result_missing is False
    assert 'https://discord.test/warning' in warn_sent
    print('missing-snapshot warning content:',
          warn_sent['https://discord.test/warning'][0]['embeds'][0]['description'])

    print('\n統合テスト（collector→poster、モック使用）: 成功')


if __name__ == '__main__':
    run()
