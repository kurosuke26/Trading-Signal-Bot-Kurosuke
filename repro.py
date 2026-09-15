#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
repro.py — 検証結果の再現性のための記録（2026-09-16追加）。

検証スクリプトの出力JSONに、以下を必ず残す：
- コードの版（gitコミット、未コミットの変更があるか、対象ファイルのハッシュ）
- データの指紋（_universe.json / _delisted.json と、全銘柄の bars.json・fins.json のハッシュを
  銘柄コード順に連結したもののハッシュ）→ 同じ指紋なら同じデータ
- 実行環境（Python・pandas・numpy のバージョン）
- パラメータ（呼び出し側から渡す）
同じコード版・同じデータ指紋・同じパラメータなら、同じ結果が出る（乱数は使っていない）。
"""

import hashlib
import json
import os
import platform
import subprocess
import sys

import numpy as np
import pandas as pd

CACHE_DIR = os.getenv('JQUANTS_CACHE_DIR') or 'data/jquants_cache'


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def code_version(files):
    info = {}
    try:
        info['git_commit'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
        info['git_dirty'] = bool(subprocess.check_output(['git', 'status', '--porcelain'], text=True).strip())
    except Exception:  # noqa: BLE001
        info['git_commit'] = None
    info['file_sha256'] = {f: _sha256_file(f)[:16] for f in sorted(files) if os.path.exists(f)}
    return info


def data_fingerprint(tickers):
    h = hashlib.sha256()
    for meta in ('_universe.json', '_delisted.json'):
        p = os.path.join(CACHE_DIR, meta)
        h.update(meta.encode())
        h.update(_sha256_file(p).encode() if os.path.exists(p) else b'-')
    for t in sorted(tickers):
        for name in ('bars.json', 'fins.json'):
            p = os.path.join(CACHE_DIR, t, name)
            h.update(f'{t}/{name}'.encode())
            h.update(_sha256_file(p).encode() if os.path.exists(p) else b'-')
    return h.hexdigest()


def run_metadata(tickers, params, files):
    return {
        'code': code_version(files),
        'data_fingerprint_sha256': data_fingerprint(tickers),
        'tickers': len(tickers),
        'environment': {'python': sys.version.split()[0], 'pandas': pd.__version__, 'numpy': np.__version__,
                        'platform': platform.platform()},
        'params': params,
    }


def dumps_params(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)
